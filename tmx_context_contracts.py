"""Frozen leaf contracts for the LocalCAT TMX context profile.

The contracts deliberately contain no Workspace, Chunk, Store, Qt, or package
implementation types.  Owners adapt their exact facts into ``TmxScopeBinding``
and ordered ``TmxExportUnit`` values before entering the TMX layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Final


TMX_CONTEXT_PROFILE_ID: Final = "localcat-tmx-level1-context-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _exact_text(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact string")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _nonnegative(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be an exact integer")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


class TmxScopeKind(str, Enum):
    MANAGED_RESOURCE = "managed_resource"
    ENTIRE_PROJECT = "entire_project"
    SELECTED_CHUNK = "selected_chunk"


class TmxCarrierKind(str, Enum):
    DIRECT = "direct"
    RESOURCE_PACKAGE = "resource_package"


class TmxLossDisposition(str, Enum):
    EXCLUDED = "excluded"
    WARNING = "warning"
    BLOCKING = "blocking"


class TmxPropScope(str, Enum):
    TU = "tu"
    SOURCE_TUV = "source_tuv"
    TARGET_TUV = "target_tuv"


class TmxDestinationBeforeKind(str, Enum):
    ABSENT = "absent"
    REGULAR = "regular"


@dataclass(frozen=True, slots=True)
class TmxEffectiveLocales:
    source_locale: str
    target_locale: str

    def __post_init__(self) -> None:
        for field in ("source_locale", "target_locale"):
            value = _exact_text(getattr(self, field), f"TmxEffectiveLocales.{field}")
            if value != value.strip() or not _LOCALE_RE.fullmatch(value):
                raise ValueError(f"TmxEffectiveLocales.{field} is not an exact locale")
            if value.casefold() == "und":
                raise ValueError(f"TmxEffectiveLocales.{field} must be explicit, not und")
        if self.source_locale.casefold() == self.target_locale.casefold():
            raise ValueError("TMX effective source and target locales must differ")


@dataclass(frozen=True, slots=True)
class TmxScopeBinding:
    scope_kind: TmxScopeKind
    scope_id: str
    binding_digest: str
    unit_count: int
    project_id: str | None = None
    chunk_plan_id: str | None = None
    chunk_plan_revision: int | None = None
    chunk_id: str | None = None
    document_count: int = 0
    attached_count: int = 0

    def __post_init__(self) -> None:
        if type(self.scope_kind) is not TmxScopeKind:
            raise TypeError("TmxScopeBinding.scope_kind must be exact TmxScopeKind")
        _exact_text(self.scope_id, "TmxScopeBinding.scope_id")
        if not _DIGEST_RE.fullmatch(_exact_text(self.binding_digest, "TmxScopeBinding.binding_digest")):
            raise ValueError("TmxScopeBinding.binding_digest must be lowercase sha256")
        _nonnegative(self.unit_count, "TmxScopeBinding.unit_count")
        _nonnegative(self.document_count, "TmxScopeBinding.document_count")
        _nonnegative(self.attached_count, "TmxScopeBinding.attached_count")
        if self.attached_count > self.unit_count:
            raise ValueError("TmxScopeBinding.attached_count exceeds unit_count")
        for field in ("project_id", "chunk_plan_id", "chunk_id"):
            value = getattr(self, field)
            if value is not None:
                _exact_text(value, f"TmxScopeBinding.{field}")
        if self.chunk_plan_revision is not None:
            _nonnegative(self.chunk_plan_revision, "TmxScopeBinding.chunk_plan_revision")
        chunk_fields = (self.chunk_plan_id, self.chunk_plan_revision, self.chunk_id)
        if self.scope_kind is TmxScopeKind.SELECTED_CHUNK:
            if any(value is None for value in chunk_fields):
                raise ValueError("selected_chunk binding requires plan, revision, and chunk id")
            if self.project_id is None:
                raise ValueError("selected_chunk binding requires project_id")
        elif any(value is not None for value in chunk_fields):
            raise ValueError("non-chunk binding cannot carry chunk facts")


@dataclass(frozen=True, slots=True)
class TmxOrderedProp:
    type: str
    value: str
    xml_lang: str | None = None
    scope: TmxPropScope = TmxPropScope.TU

    def __post_init__(self) -> None:
        _exact_text(self.type, "TmxOrderedProp.type")
        _exact_text(self.value, "TmxOrderedProp.value", allow_empty=True)
        if self.xml_lang is not None:
            _exact_text(self.xml_lang, "TmxOrderedProp.xml_lang")
        if type(self.scope) is not TmxPropScope:
            raise TypeError("TmxOrderedProp.scope must be exact TmxPropScope")


@dataclass(frozen=True, slots=True)
class TmxProvenanceEntry:
    key: str
    value: str

    def __post_init__(self) -> None:
        _exact_text(self.key, "TmxProvenanceEntry.key")
        _exact_text(self.value, "TmxProvenanceEntry.value", allow_empty=True)


@dataclass(frozen=True, slots=True)
class TmxExportUnit:
    unit_identity: str
    source: str
    target: str
    confirmed: bool
    attached: bool = True
    speaker: str | None = None
    context_prev: str | None = None
    context_next: str | None = None
    file_source: str | None = None
    status: str | None = None
    provenance: tuple[TmxProvenanceEntry, ...] = ()
    imported_props: tuple[TmxOrderedProp, ...] = ()
    has_inline_xml: bool = False

    def __post_init__(self) -> None:
        _exact_text(self.unit_identity, "TmxExportUnit.unit_identity")
        _exact_text(self.source, "TmxExportUnit.source", allow_empty=True)
        _exact_text(self.target, "TmxExportUnit.target", allow_empty=True)
        if type(self.confirmed) is not bool or type(self.attached) is not bool:
            raise TypeError("TmxExportUnit confirmed/attached must be exact bool")
        if type(self.has_inline_xml) is not bool:
            raise TypeError("TmxExportUnit.has_inline_xml must be exact bool")
        for field in ("speaker", "context_prev", "context_next", "file_source", "status"):
            value = getattr(self, field)
            if value is not None:
                _exact_text(value, f"TmxExportUnit.{field}", allow_empty=True)
        if type(self.provenance) is not tuple or type(self.imported_props) is not tuple:
            raise TypeError("TmxExportUnit ordered metadata must use tuples")
        for entry in self.provenance:
            if type(entry) is not TmxProvenanceEntry:
                raise TypeError("TmxExportUnit.provenance entries must be exact")
        for prop in self.imported_props:
            if type(prop) is not TmxOrderedProp:
                raise TypeError("TmxExportUnit.imported_props entries must be exact")


@dataclass(frozen=True, slots=True)
class TmxLossCount:
    code: str
    disposition: TmxLossDisposition
    count: int

    def __post_init__(self) -> None:
        if not _SAFE_CODE_RE.fullmatch(_exact_text(self.code, "TmxLossCount.code")):
            raise ValueError("TmxLossCount.code is not a stable safe code")
        if type(self.disposition) is not TmxLossDisposition:
            raise TypeError("TmxLossCount.disposition must be exact")
        if _nonnegative(self.count, "TmxLossCount.count") == 0:
            raise ValueError("TmxLossCount.count must be positive")


@dataclass(frozen=True, slots=True)
class TmxSafeIssue:
    code: str
    disposition: TmxLossDisposition
    unit_ordinal: int | None = None

    def __post_init__(self) -> None:
        if not _SAFE_CODE_RE.fullmatch(_exact_text(self.code, "TmxSafeIssue.code")):
            raise ValueError("TmxSafeIssue.code is not stable")
        if type(self.disposition) is not TmxLossDisposition:
            raise TypeError("TmxSafeIssue.disposition must be exact")
        if self.unit_ordinal is not None:
            _nonnegative(self.unit_ordinal, "TmxSafeIssue.unit_ordinal")


@dataclass(frozen=True, slots=True)
class TmxLossReport:
    included_count: int
    excluded_count: int
    warning_count: int
    blocking_count: int
    counts: tuple[TmxLossCount, ...]
    issues: tuple[TmxSafeIssue, ...]
    issues_truncated: bool = False

    def __post_init__(self) -> None:
        for field in ("included_count", "excluded_count", "warning_count", "blocking_count"):
            _nonnegative(getattr(self, field), f"TmxLossReport.{field}")
        if type(self.counts) is not tuple or type(self.issues) is not tuple:
            raise TypeError("TmxLossReport counts/issues must use tuples")
        if len(self.issues) > 32:
            raise ValueError("TmxLossReport.issues exceeds public bound")
        for value in self.counts:
            if type(value) is not TmxLossCount:
                raise TypeError("TmxLossReport.counts entries must be exact")
        for value in self.issues:
            if type(value) is not TmxSafeIssue:
                raise TypeError("TmxLossReport.issues entries must be exact")
        if type(self.issues_truncated) is not bool:
            raise TypeError("TmxLossReport.issues_truncated must be exact bool")


@dataclass(frozen=True, slots=True)
class TmxPayloadProof:
    profile_id: str
    effective_locales: TmxEffectiveLocales
    payload_digest: str
    parser_content_digest: str
    included_count: int
    prop_count: int
    loss_report: TmxLossReport

    def __post_init__(self) -> None:
        if self.profile_id != TMX_CONTEXT_PROFILE_ID:
            raise ValueError("unsupported TMX profile")
        if type(self.effective_locales) is not TmxEffectiveLocales:
            raise TypeError("TmxPayloadProof.effective_locales must be exact")
        for field in ("payload_digest", "parser_content_digest"):
            if not _DIGEST_RE.fullmatch(_exact_text(getattr(self, field), f"TmxPayloadProof.{field}")):
                raise ValueError(f"TmxPayloadProof.{field} must be lowercase sha256")
        _nonnegative(self.included_count, "TmxPayloadProof.included_count")
        _nonnegative(self.prop_count, "TmxPayloadProof.prop_count")
        if type(self.loss_report) is not TmxLossReport:
            raise TypeError("TmxPayloadProof.loss_report must be exact")


@dataclass(frozen=True, slots=True)
class TmxPreparedPayload:
    data: bytes
    proof: TmxPayloadProof
    scope_kind: TmxScopeKind
    scope_id: str
    binding_digest: str

    def __post_init__(self) -> None:
        if type(self.data) is not bytes:
            raise TypeError("TmxPreparedPayload.data must be exact bytes")
        if type(self.proof) is not TmxPayloadProof:
            raise TypeError("TmxPreparedPayload.proof must be exact")
        if type(self.scope_kind) is not TmxScopeKind:
            raise TypeError("TmxPreparedPayload.scope_kind must be exact")
        _exact_text(self.scope_id, "TmxPreparedPayload.scope_id")
        if not _DIGEST_RE.fullmatch(self.binding_digest):
            raise ValueError("TmxPreparedPayload.binding_digest must be lowercase sha256")


@dataclass(frozen=True, slots=True)
class TmxExportPreview:
    operation_id: str
    scope_kind: TmxScopeKind
    scope_id: str
    project_id: str | None
    chunk_plan_id: str | None
    chunk_plan_revision: int | None
    chunk_id: str | None
    document_count: int
    attached_count: int
    included_count: int
    excluded_count: int
    warning_count: int
    loss_counts: tuple[TmxLossCount, ...]
    safe_issues: tuple[TmxSafeIssue, ...]
    effective_locales: TmxEffectiveLocales
    profile_id: str
    destination: Path
    destination_before: TmxDestinationBeforeKind
    destination_before_digest: str | None

    def __post_init__(self) -> None:
        _exact_text(self.operation_id, "TmxExportPreview.operation_id")
        if type(self.scope_kind) is not TmxScopeKind:
            raise TypeError("TmxExportPreview.scope_kind must be exact")
        _exact_text(self.scope_id, "TmxExportPreview.scope_id")
        if not isinstance(self.destination, Path) or not self.destination.is_absolute():
            raise ValueError("TmxExportPreview.destination must be an absolute Path")
        if type(self.effective_locales) is not TmxEffectiveLocales:
            raise TypeError("TmxExportPreview.effective_locales must be exact")
        if self.profile_id != TMX_CONTEXT_PROFILE_ID:
            raise ValueError("unsupported TMX preview profile")
        if type(self.destination_before) is not TmxDestinationBeforeKind:
            raise TypeError("TmxExportPreview.destination_before must be exact")
        if self.destination_before_digest is not None and not _DIGEST_RE.fullmatch(self.destination_before_digest):
            raise ValueError("destination before digest must be lowercase sha256")
        for field in ("document_count", "attached_count", "included_count", "excluded_count", "warning_count"):
            _nonnegative(getattr(self, field), f"TmxExportPreview.{field}")
        if type(self.loss_counts) is not tuple or type(self.safe_issues) is not tuple:
            raise TypeError("TmxExportPreview loss fields must use tuples")


@dataclass(frozen=True, slots=True)
class TmxDirectReceipt:
    operation_id: str
    scope_kind: TmxScopeKind
    scope_id: str
    profile_id: str
    effective_locales: TmxEffectiveLocales
    destination: Path
    destination_before: TmxDestinationBeforeKind
    before_digest: str | None
    after_digest: str
    included_count: int
    excluded_count: int
    warning_count: int
    loss_counts: tuple[TmxLossCount, ...]
    durable: bool

    def __post_init__(self) -> None:
        _exact_text(self.operation_id, "TmxDirectReceipt.operation_id")
        _exact_text(self.scope_id, "TmxDirectReceipt.scope_id")
        if self.profile_id != TMX_CONTEXT_PROFILE_ID:
            raise ValueError("unsupported TMX receipt profile")
        if type(self.scope_kind) is not TmxScopeKind:
            raise TypeError("TmxDirectReceipt.scope_kind must be exact")
        if type(self.effective_locales) is not TmxEffectiveLocales:
            raise TypeError("TmxDirectReceipt.effective_locales must be exact")
        if not isinstance(self.destination, Path) or not self.destination.is_absolute():
            raise ValueError("TmxDirectReceipt.destination must be absolute")
        if type(self.destination_before) is not TmxDestinationBeforeKind:
            raise TypeError("TmxDirectReceipt.destination_before must be exact")
        if self.before_digest is not None and not _DIGEST_RE.fullmatch(self.before_digest):
            raise ValueError("TmxDirectReceipt.before_digest must be sha256")
        if not _DIGEST_RE.fullmatch(self.after_digest):
            raise ValueError("TmxDirectReceipt.after_digest must be sha256")
        for field in ("included_count", "excluded_count", "warning_count"):
            _nonnegative(getattr(self, field), f"TmxDirectReceipt.{field}")
        if type(self.loss_counts) is not tuple:
            raise TypeError("TmxDirectReceipt.loss_counts must use a tuple")
        if type(self.durable) is not bool:
            raise TypeError("TmxDirectReceipt.durable must be exact bool")


class TmxContextError(RuntimeError):
    """Body-safe TMX domain failure."""

    def __init__(self, code: str, safe_summary: str, *, loss_report: TmxLossReport | None = None) -> None:
        _exact_text(code, "TmxContextError.code")
        _exact_text(safe_summary, "TmxContextError.safe_summary")
        self.code = code
        self.safe_summary = safe_summary
        self.loss_report = loss_report
        super().__init__(f"{code}: {safe_summary}")


class TmxDirectPlan:
    """Opaque, non-serializable, single-use direct-publication authority."""

    __slots__ = ("_operation_id", "_state", "_payload", "_binding", "_preview", "_destination_fact")

    def __init__(self, *, operation_id: str, payload: TmxPreparedPayload, binding: TmxScopeBinding,
                 preview: TmxExportPreview, destination_fact: object) -> None:
        self._operation_id = operation_id
        self._payload = payload
        self._binding = binding
        self._preview = preview
        self._destination_fact = destination_fact
        self._state = "issued"

    def __repr__(self) -> str:
        return "<TmxDirectPlan private single-use authority>"

    def __reduce__(self):
        raise TypeError("TmxDirectPlan is not serializable")

    def _consume(self) -> tuple[TmxPreparedPayload, TmxScopeBinding, TmxExportPreview, object]:
        if self._state != "issued":
            raise TmxContextError("TMX.PLAN_CONSUMED", "direct export plan is no longer usable")
        self._state = "consumed"
        return self._payload, self._binding, self._preview, self._destination_fact
