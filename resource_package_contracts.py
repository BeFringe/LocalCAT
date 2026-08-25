"""Leaf contracts for versioned LocalCAT resource snapshots and packages."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Callable


MANIFEST_SCHEMA = "localcat-resource-package-manifest-v1"
CARRIER_PROFILE = "localcat-resource-package-zip-v1"
PAYLOAD_PROFILE_SET = "localcat-resource-payload-set-v1"
LIMIT_PROFILE = "localcat-resource-package-limits-v1"
MANIFEST_SCHEMA_V2 = "localcat-resource-package-manifest-v2"
CARRIER_PROFILE_V2 = "localcat-resource-package-zip-v2"
PAYLOAD_PROFILE_SET_V2 = "localcat-resource-payload-set-v2"
LIMIT_PROFILE_V2 = "localcat-resource-package-limits-v2"
RECEIPT_SCHEMA = "localcat-resource-operation-receipt-v1"
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_SAFE_ISSUES = 256
MAX_JSON_DEPTH = 32
MAX_MEMBER_PATH_BYTES = 1024
MAX_MEMBER_SEGMENT_BYTES = 255


class PortableResourceKind(str, Enum):
    TRANSLATION_MEMORY = "translation_memory"
    TERMBASE = "termbase"


class ResourcePayloadProfile(str, Enum):
    TM_JSONL_V1 = "localcat-tm-jsonl-v1"
    TERMBASE_CSV_V1 = "localcat-termbase-csv-v1"
    TMX_LEVEL1_CONTEXT_V1 = "localcat-tmx-level1-context-v1"


class ResourcePackageSourceScope(str, Enum):
    """Source-scope axis used only by the package capability matrix."""

    MANAGED_RESOURCE = "managed_resource"
    ENTIRE_PROJECT = "entire_project"
    SELECTED_CHUNK = "selected_chunk"


class ResourceImportMode(str, Enum):
    CREATE_NEW = "create_new"
    REPLACE_SELECTED = "replace_selected"


class ResourceOperationKind(str, Enum):
    EXPORT_DIRECT = "export_direct"
    EXPORT_PACKAGE = "export_package"
    VALIDATE_PACKAGE = "validate_package"
    IMPORT_PACKAGE = "import_package"
    RECOVER = "recover"


class ResourceDurableState(str, Enum):
    COMMITTED = "committed"
    RECOVERY_REQUIRED = "recovery_required"


class ResourceRecoveryAction(str, Enum):
    COMPLETE = "complete"
    ROLLBACK = "rollback"


class ResourceRecoveryDisposition(str, Enum):
    COMPLETE_AVAILABLE = "complete_available"
    ROLLBACK_AVAILABLE = "rollback_available"
    MANUAL_REQUIRED = "manual_required"


class ResourcePortabilityError(RuntimeError):
    """Body-free public error carrying one stable code."""

    __slots__ = ("code", "retryable")

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        _require_nonempty(code, "resource portability error code")
        _require_bool(retryable, "resource portability retryable")
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ResourcePackageLimitProfile:
    profile: str = LIMIT_PROFILE
    artifact_bytes: int = MAX_ARTIFACT_BYTES
    payload_bytes: int = MAX_PAYLOAD_BYTES
    manifest_bytes: int = MAX_MANIFEST_BYTES
    member_count: int = 2
    retained_safe_issues: int = MAX_SAFE_ISSUES
    json_depth: int = MAX_JSON_DEPTH

    def __post_init__(self) -> None:
        if self.profile not in (LIMIT_PROFILE, LIMIT_PROFILE_V2):
            raise ValueError("RESOURCE.PORTABILITY.LIMIT_PROFILE_UNSUPPORTED")
        for name, value in (
            ("artifact bytes", self.artifact_bytes),
            ("payload bytes", self.payload_bytes),
            ("manifest bytes", self.manifest_bytes),
            ("member count", self.member_count),
            ("safe issues", self.retained_safe_issues),
            ("JSON depth", self.json_depth),
        ):
            _require_int(value, name, minimum=1)
        if self.member_count != 2:
            raise ValueError("RESOURCE.PORTABILITY.MEMBER_COUNT_INVALID")


@dataclass(frozen=True, slots=True)
class ResourcePayloadDescriptor:
    path: str
    sha256: str
    byte_count: int
    record_count: int

    def __post_init__(self) -> None:
        _validate_member_path(self.path)
        _require_digest(self.sha256, "payload digest")
        _require_int(self.byte_count, "payload byte count", minimum=0)
        _require_int(self.record_count, "payload record count", minimum=0)
        if self.byte_count > MAX_PAYLOAD_BYTES:
            raise ValueError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")


@dataclass(frozen=True, slots=True)
class ResourceProfileCounts:
    legacy_record_count: int
    v1_record_count: int

    def __post_init__(self) -> None:
        _require_int(self.legacy_record_count, "legacy record count", minimum=0)
        _require_int(self.v1_record_count, "v1 record count", minimum=0)


@dataclass(frozen=True, slots=True)
class ResourcePackageManifest:
    schema: str
    carrier_profile: str
    payload_profile_set: str
    resource_kind: PortableResourceKind
    payload_profile: ResourcePayloadProfile
    payload: ResourcePayloadDescriptor
    profile_counts: ResourceProfileCounts

    def __post_init__(self) -> None:
        expected_triple = package_profile_triple_for_payload(self.payload_profile)
        if self.schema != expected_triple[0]:
            raise ValueError("RESOURCE.PACKAGE.MANIFEST_INVALID")
        if self.carrier_profile != expected_triple[1]:
            raise ValueError("RESOURCE.PACKAGE.FORMAT_UNSUPPORTED")
        if self.payload_profile_set != expected_triple[2]:
            raise ValueError("RESOURCE.PORTABILITY.PROFILE_UNSUPPORTED")
        if type(self.resource_kind) is not PortableResourceKind:
            raise TypeError("resource kind must be exact PortableResourceKind")
        if type(self.payload_profile) is not ResourcePayloadProfile:
            raise TypeError("payload profile must be exact ResourcePayloadProfile")
        if type(self.payload) is not ResourcePayloadDescriptor:
            raise TypeError("payload descriptor must be exact")
        if type(self.profile_counts) is not ResourceProfileCounts:
            raise TypeError("profile counts must be exact")
        self.payload.__post_init__()
        self.profile_counts.__post_init__()
        if not payload_profile_supports_kind(
            self.payload_profile,
            self.resource_kind,
        ):
            raise ValueError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        if self.payload.path != payload_path_for_profile(self.payload_profile):
            raise ValueError("RESOURCE.PACKAGE.MANIFEST_INVALID")
        if self.resource_kind is PortableResourceKind.TRANSLATION_MEMORY:
            if self.profile_counts != ResourceProfileCounts(0, 0):
                raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")
        elif (
            self.profile_counts.legacy_record_count
            + self.profile_counts.v1_record_count
            != self.payload.record_count
        ):
            raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")


@dataclass(frozen=True, slots=True)
class PortableResourceSnapshot:
    kind: PortableResourceKind
    profile: ResourcePayloadProfile
    payload_digest: str
    payload_byte_count: int
    record_count: int
    legacy_record_count: int
    v1_record_count: int
    source_baseline_digest: str
    owner_receipt_digest: str | None
    owner_generation: int | None = None
    owner_revision: int | None = None
    safe_issues: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not PortableResourceKind:
            raise TypeError("snapshot kind must be exact")
        if type(self.profile) is not ResourcePayloadProfile:
            raise TypeError("snapshot profile must be exact")
        if not payload_profile_supports_kind(self.profile, self.kind):
            raise ValueError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        _require_digest(self.payload_digest, "snapshot payload digest")
        _require_digest(self.source_baseline_digest, "snapshot baseline digest")
        _require_optional_digest(self.owner_receipt_digest, "owner receipt digest")
        for name, value in (
            ("payload byte count", self.payload_byte_count),
            ("record count", self.record_count),
            ("legacy record count", self.legacy_record_count),
            ("v1 record count", self.v1_record_count),
        ):
            _require_int(value, name, minimum=0)
        if self.payload_byte_count > MAX_PAYLOAD_BYTES:
            raise ValueError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        _require_optional_int(self.owner_generation, "owner generation")
        _require_optional_int(self.owner_revision, "owner revision")
        _require_safe_codes(self.safe_issues)
        if self.kind is PortableResourceKind.TRANSLATION_MEMORY:
            if self.legacy_record_count or self.v1_record_count:
                raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")
        elif self.legacy_record_count + self.v1_record_count != self.record_count:
            raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")


@dataclass(frozen=True, slots=True)
class ResourcePackageValidationReport:
    artifact_digest: str
    artifact_byte_count: int
    manifest_digest: str
    carrier_profile: str
    payload_profile_set: str
    resource_kind: PortableResourceKind
    payload_profile: ResourcePayloadProfile
    payload_digest: str
    payload_byte_count: int
    record_count: int
    legacy_record_count: int
    v1_record_count: int
    safe_issues: tuple[str, ...]

    def __post_init__(self) -> None:
        for digest, name in (
            (self.artifact_digest, "artifact digest"),
            (self.manifest_digest, "manifest digest"),
            (self.payload_digest, "payload digest"),
        ):
            _require_digest(digest, name)
        for value, name in (
            (self.artifact_byte_count, "artifact byte count"),
            (self.payload_byte_count, "payload byte count"),
            (self.record_count, "record count"),
            (self.legacy_record_count, "legacy record count"),
            (self.v1_record_count, "v1 record count"),
        ):
            _require_int(value, name, minimum=0)
        if self.artifact_byte_count > MAX_ARTIFACT_BYTES:
            raise ValueError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        if self.payload_byte_count > MAX_PAYLOAD_BYTES:
            raise ValueError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
        expected_triple = package_profile_triple_for_payload(self.payload_profile)
        if self.carrier_profile != expected_triple[1]:
            raise ValueError("RESOURCE.PACKAGE.FORMAT_UNSUPPORTED")
        if self.payload_profile_set != expected_triple[2]:
            raise ValueError("RESOURCE.PORTABILITY.PROFILE_UNSUPPORTED")
        if type(self.resource_kind) is not PortableResourceKind:
            raise TypeError("validation resource kind must be exact")
        if type(self.payload_profile) is not ResourcePayloadProfile:
            raise TypeError("validation payload profile must be exact")
        if not payload_profile_supports_kind(
            self.payload_profile,
            self.resource_kind,
        ):
            raise ValueError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        if self.resource_kind is PortableResourceKind.TRANSLATION_MEMORY:
            if self.legacy_record_count or self.v1_record_count:
                raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")
        elif self.legacy_record_count + self.v1_record_count != self.record_count:
            raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")
        _require_safe_codes(self.safe_issues)


@dataclass(frozen=True, slots=True)
class ResourcePackageImportPreview:
    operation_id: str
    mode: ResourceImportMode
    validation: ResourcePackageValidationReport
    destination_exists: bool
    destination_resource_id: str | None
    safe_warnings: tuple[str, ...]
    blocking_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.operation_id, "preview operation id")
        if type(self.mode) is not ResourceImportMode:
            raise TypeError("preview mode must be exact")
        if type(self.validation) is not ResourcePackageValidationReport:
            raise TypeError("preview validation must be exact")
        self.validation.__post_init__()
        _require_bool(self.destination_exists, "destination exists")
        _require_optional_nonempty(
            self.destination_resource_id,
            "destination resource id",
        )
        if self.mode is ResourceImportMode.CREATE_NEW:
            if self.destination_exists or self.destination_resource_id is not None:
                raise ValueError("RESOURCE.IMPORT.DESTINATION_INVALID")
        elif not self.destination_exists or self.destination_resource_id is None:
            raise ValueError("RESOURCE.IMPORT.DESTINATION_INVALID")
        _require_safe_codes(self.safe_warnings)
        _require_safe_codes(self.blocking_reasons)


@dataclass(frozen=True, slots=True)
class ResourceOperationReceipt:
    receipt_schema: str
    operation_id: str
    operation_kind: ResourceOperationKind
    resource_kind: PortableResourceKind
    payload_profile: ResourcePayloadProfile
    source_resource_id: str | None
    destination_resource_id: str | None
    package_artifact_digest: str | None
    payload_digest: str
    destination_before_digest: str | None
    destination_after_digest: str | None
    record_count: int
    legacy_record_count: int
    v1_record_count: int
    skipped_count: int
    safe_warnings: tuple[str, ...]
    durable_state: ResourceDurableState
    owner_generation: int | None = None
    owner_revision: int | None = None
    owner_receipt_digest: str | None = None
    source_baseline_digest: str | None = None

    def __post_init__(self) -> None:
        if self.receipt_schema != RECEIPT_SCHEMA:
            raise ValueError("RESOURCE.RECEIPT.INVALID")
        _require_nonempty(self.operation_id, "receipt operation id")
        if type(self.operation_kind) is not ResourceOperationKind:
            raise TypeError("receipt operation kind must be exact")
        if type(self.resource_kind) is not PortableResourceKind:
            raise TypeError("receipt resource kind must be exact")
        if type(self.payload_profile) is not ResourcePayloadProfile:
            raise TypeError("receipt profile must be exact")
        if not payload_profile_supports_kind(
            self.payload_profile,
            self.resource_kind,
        ):
            raise ValueError("RESOURCE.PORTABILITY.KIND_MISMATCH")
        _require_optional_nonempty(self.source_resource_id, "source resource id")
        _require_optional_nonempty(
            self.destination_resource_id,
            "destination resource id",
        )
        for value, name in (
            (self.package_artifact_digest, "package artifact digest"),
            (self.destination_before_digest, "destination before digest"),
            (self.destination_after_digest, "destination after digest"),
            (self.owner_receipt_digest, "owner receipt digest"),
            (self.source_baseline_digest, "source baseline digest"),
        ):
            _require_optional_digest(value, name)
        _require_digest(self.payload_digest, "receipt payload digest")
        for value, name in (
            (self.record_count, "record count"),
            (self.legacy_record_count, "legacy record count"),
            (self.v1_record_count, "v1 record count"),
            (self.skipped_count, "skipped count"),
        ):
            _require_int(value, name, minimum=0)
        _require_safe_codes(self.safe_warnings)
        if type(self.durable_state) is not ResourceDurableState:
            raise TypeError("receipt durable state must be exact")
        _require_optional_int(self.owner_generation, "owner generation")
        _require_optional_int(self.owner_revision, "owner revision")
        if self.durable_state is ResourceDurableState.COMMITTED:
            if self.skipped_count:
                raise ValueError("RESOURCE.EXPORT.SNAPSHOT_INCOMPLETE")
            _require_digest(
                self.destination_after_digest,
                "committed destination digest",
            )
        if self.resource_kind is PortableResourceKind.TERMBASE:
            if self.legacy_record_count + self.v1_record_count != self.record_count:
                raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")
        elif self.legacy_record_count or self.v1_record_count:
            raise ValueError("RESOURCE.PACKAGE.COUNT_MISMATCH")
        if self.operation_kind in (
            ResourceOperationKind.EXPORT_DIRECT,
            ResourceOperationKind.EXPORT_PACKAGE,
        ):
            if self.source_resource_id is None or self.destination_resource_id is not None:
                raise ValueError("RESOURCE.RECEIPT.INVALID")
        elif self.operation_kind is ResourceOperationKind.IMPORT_PACKAGE:
            if self.source_resource_id is not None or self.destination_resource_id is None:
                raise ValueError("RESOURCE.RECEIPT.INVALID")
        if self.operation_kind is ResourceOperationKind.EXPORT_DIRECT:
            if self.package_artifact_digest is not None:
                raise ValueError("RESOURCE.RECEIPT.INVALID")
        elif self.operation_kind in (
            ResourceOperationKind.EXPORT_PACKAGE,
            ResourceOperationKind.IMPORT_PACKAGE,
        ) and self.package_artifact_digest is None:
            raise ValueError("RESOURCE.RECEIPT.INVALID")


@dataclass(frozen=True, slots=True)
class ResourceExportOutcome:
    receipt: ResourceOperationReceipt
    destination_preserved: bool

    def __post_init__(self) -> None:
        if type(self.receipt) is not ResourceOperationReceipt:
            raise TypeError("export outcome receipt must be exact")
        self.receipt.__post_init__()
        _require_bool(self.destination_preserved, "destination preserved")
        if self.receipt.operation_kind not in (
            ResourceOperationKind.EXPORT_DIRECT,
            ResourceOperationKind.EXPORT_PACKAGE,
        ):
            raise ValueError("resource export outcome requires export receipt")


@dataclass(frozen=True, slots=True)
class ResourcePackageImportResult:
    receipt: ResourceOperationReceipt
    destination_resource_id: str

    def __post_init__(self) -> None:
        if type(self.receipt) is not ResourceOperationReceipt:
            raise TypeError("import result receipt must be exact")
        self.receipt.__post_init__()
        _require_nonempty(self.destination_resource_id, "destination resource id")
        if self.receipt.operation_kind is not ResourceOperationKind.IMPORT_PACKAGE:
            raise ValueError("resource import result requires import receipt")
        if self.receipt.destination_resource_id != self.destination_resource_id:
            raise ValueError("resource import result destination must match receipt")


@dataclass(frozen=True, slots=True)
class ResourceRecoveryPreview:
    operation_id: str
    operation_kind: ResourceOperationKind
    disposition: ResourceRecoveryDisposition
    destination_resource_id: str | None
    safe_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_nonempty(self.operation_id, "recovery operation id")
        if type(self.operation_kind) is not ResourceOperationKind:
            raise TypeError("recovery operation kind must be exact")
        if type(self.disposition) is not ResourceRecoveryDisposition:
            raise TypeError("recovery disposition must be exact")
        _require_optional_nonempty(
            self.destination_resource_id,
            "recovery destination resource id",
        )
        _require_safe_codes(self.safe_reasons)


@dataclass(frozen=True, slots=True)
class ResourceRecoveryOutcome:
    operation_id: str
    action: ResourceRecoveryAction
    receipt: ResourceOperationReceipt | None

    def __post_init__(self) -> None:
        _require_nonempty(self.operation_id, "recovery operation id")
        if type(self.action) is not ResourceRecoveryAction:
            raise TypeError("recovery action must be exact")
        if self.receipt is not None:
            if type(self.receipt) is not ResourceOperationReceipt:
                raise TypeError("recovery receipt must be exact")
            self.receipt.__post_init__()
            if self.receipt.operation_id != self.operation_id:
                raise ValueError("RESOURCE.RECEIPT.INVALID")
        if self.action is ResourceRecoveryAction.COMPLETE and self.receipt is None:
            raise ValueError("RESOURCE.RECEIPT.INVALID")
        if self.action is ResourceRecoveryAction.ROLLBACK and self.receipt is not None:
            raise ValueError("RESOURCE.RECEIPT.INVALID")


@dataclass(frozen=True, slots=True)
class ResourcePackageTransferMetadata:
    artifact_sha256: str
    artifact_byte_count: int
    manifest_schema: str
    carrier_profile: str
    payload_profile_set: str
    resource_kind: PortableResourceKind
    payload_profile: ResourcePayloadProfile
    payload_sha256: str
    record_count: int

    def __post_init__(self) -> None:
        _require_digest(self.artifact_sha256, "transfer artifact digest")
        _require_digest(self.payload_sha256, "transfer payload digest")
        _require_int(self.artifact_byte_count, "transfer artifact bytes", minimum=0)
        _require_int(self.record_count, "transfer record count", minimum=0)
        expected_triple = package_profile_triple_for_payload(self.payload_profile)
        if self.manifest_schema != expected_triple[0]:
            raise ValueError("RESOURCE.PACKAGE.MANIFEST_INVALID")
        if self.carrier_profile != expected_triple[1]:
            raise ValueError("RESOURCE.PACKAGE.FORMAT_UNSUPPORTED")
        if self.payload_profile_set != expected_triple[2]:
            raise ValueError("RESOURCE.PORTABILITY.PROFILE_UNSUPPORTED")
        if type(self.resource_kind) is not PortableResourceKind:
            raise TypeError("transfer resource kind must be exact")
        if type(self.payload_profile) is not ResourcePayloadProfile:
            raise TypeError("transfer payload profile must be exact")
        if not payload_profile_supports_kind(
            self.payload_profile,
            self.resource_kind,
        ):
            raise ValueError("RESOURCE.PORTABILITY.KIND_MISMATCH")


def profile_for_kind(kind: PortableResourceKind) -> ResourcePayloadProfile:
    """Return the unchanged v1 default profile for existing callers."""

    if type(kind) is not PortableResourceKind:
        raise TypeError("resource kind must be exact")
    return (
        ResourcePayloadProfile.TM_JSONL_V1
        if kind is PortableResourceKind.TRANSLATION_MEMORY
        else ResourcePayloadProfile.TERMBASE_CSV_V1
    )


def payload_path_for_profile(profile: ResourcePayloadProfile) -> str:
    if type(profile) is not ResourcePayloadProfile:
        raise TypeError("payload profile must be exact")
    if profile is ResourcePayloadProfile.TM_JSONL_V1:
        return "payload/tm.jsonl"
    if profile is ResourcePayloadProfile.TERMBASE_CSV_V1:
        return "payload/termbase.csv"
    return "payload/resource.tmx"


def payload_profile_supports_kind(
    profile: ResourcePayloadProfile,
    kind: PortableResourceKind,
) -> bool:
    if type(profile) is not ResourcePayloadProfile:
        raise TypeError("payload profile must be exact")
    if type(kind) is not PortableResourceKind:
        raise TypeError("resource kind must be exact")
    if kind is PortableResourceKind.TERMBASE:
        return profile is ResourcePayloadProfile.TERMBASE_CSV_V1
    return profile in (
        ResourcePayloadProfile.TM_JSONL_V1,
        ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1,
    )


def package_profile_triple_for_payload(
    profile: ResourcePayloadProfile,
) -> tuple[str, str, str]:
    if type(profile) is not ResourcePayloadProfile:
        raise TypeError("payload profile must be exact")
    if profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1:
        return (MANIFEST_SCHEMA_V2, CARRIER_PROFILE_V2, PAYLOAD_PROFILE_SET_V2)
    return (MANIFEST_SCHEMA, CARRIER_PROFILE, PAYLOAD_PROFILE_SET)


def limit_profile_for_payload(profile: ResourcePayloadProfile) -> str:
    if type(profile) is not ResourcePayloadProfile:
        raise TypeError("payload profile must be exact")
    return (
        LIMIT_PROFILE_V2
        if profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1
        else LIMIT_PROFILE
    )


def resource_package_capability(
    scope: ResourcePackageSourceScope,
    kind: PortableResourceKind,
    profile: ResourcePayloadProfile,
    *,
    importing: bool,
) -> bool:
    """Return the exact scope/kind/profile capability without inferring scope."""

    if type(scope) is not ResourcePackageSourceScope:
        raise TypeError("package source scope must be exact")
    if type(kind) is not PortableResourceKind:
        raise TypeError("resource kind must be exact")
    if type(profile) is not ResourcePayloadProfile:
        raise TypeError("payload profile must be exact")
    if type(importing) is not bool:
        raise TypeError("package importing flag must be exact")
    if scope is not ResourcePackageSourceScope.MANAGED_RESOURCE:
        return False
    if not payload_profile_supports_kind(profile, kind):
        return False
    if importing and profile is ResourcePayloadProfile.TMX_LEVEL1_CONTEXT_V1:
        return False
    return True


def manifest_to_bytes(manifest: ResourcePackageManifest) -> bytes:
    if type(manifest) is not ResourcePackageManifest:
        raise TypeError("manifest must be exact ResourcePackageManifest")
    manifest.__post_init__()
    value = {
        "schema": manifest.schema,
        "carrier_profile": manifest.carrier_profile,
        "payload_profile_set": manifest.payload_profile_set,
        "resource": {
            "kind": manifest.resource_kind.value,
            "payload_profile": manifest.payload_profile.value,
            "payload": {
                "path": manifest.payload.path,
                "sha256": manifest.payload.sha256,
                "byte_count": manifest.payload.byte_count,
                "record_count": manifest.payload.record_count,
            },
            "profile_counts": {
                "legacy_record_count": manifest.profile_counts.legacy_record_count,
                "v1_record_count": manifest.profile_counts.v1_record_count,
            },
        },
    }
    payload = _canonical_json(value)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ResourcePortabilityError("RESOURCE.PORTABILITY.LIMIT_EXCEEDED")
    return payload


def manifest_from_bytes(payload: bytes) -> ResourcePackageManifest:
    value = _decode_exact_json(payload, "RESOURCE.PACKAGE.MANIFEST_INVALID")
    _require_exact_keys(
        value,
        ("schema", "carrier_profile", "payload_profile_set", "resource"),
    )
    resource = _require_dict(value["resource"])
    _require_exact_keys(
        resource,
        ("kind", "payload_profile", "payload", "profile_counts"),
    )
    descriptor = _require_dict(resource["payload"])
    _require_exact_keys(
        descriptor,
        ("path", "sha256", "byte_count", "record_count"),
    )
    counts = _require_dict(resource["profile_counts"])
    _require_exact_keys(counts, ("legacy_record_count", "v1_record_count"))
    try:
        manifest = ResourcePackageManifest(
            schema=_require_str(value["schema"]),
            carrier_profile=_require_str(value["carrier_profile"]),
            payload_profile_set=_require_str(value["payload_profile_set"]),
            resource_kind=PortableResourceKind(_require_str(resource["kind"])),
            payload_profile=ResourcePayloadProfile(
                _require_str(resource["payload_profile"])
            ),
            payload=ResourcePayloadDescriptor(
                path=_require_str(descriptor["path"]),
                sha256=_require_str(descriptor["sha256"]),
                byte_count=_require_exact_int(descriptor["byte_count"]),
                record_count=_require_exact_int(descriptor["record_count"]),
            ),
            profile_counts=ResourceProfileCounts(
                legacy_record_count=_require_exact_int(
                    counts["legacy_record_count"]
                ),
                v1_record_count=_require_exact_int(counts["v1_record_count"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ResourcePortabilityError(
            "RESOURCE.PACKAGE.MANIFEST_INVALID"
        ) from error
    if manifest_to_bytes(manifest) != payload:
        raise ResourcePortabilityError("RESOURCE.PACKAGE.MANIFEST_INVALID")
    return manifest


def receipt_to_bytes(receipt: ResourceOperationReceipt) -> bytes:
    if type(receipt) is not ResourceOperationReceipt:
        raise TypeError("receipt must be exact ResourceOperationReceipt")
    receipt.__post_init__()
    return _canonical_json(
        {
            "receipt_schema": receipt.receipt_schema,
            "operation_id": receipt.operation_id,
            "operation_kind": receipt.operation_kind.value,
            "resource_kind": receipt.resource_kind.value,
            "payload_profile": receipt.payload_profile.value,
            "source_resource_id": receipt.source_resource_id,
            "destination_resource_id": receipt.destination_resource_id,
            "package_artifact_digest": receipt.package_artifact_digest,
            "payload_digest": receipt.payload_digest,
            "destination_before_digest": receipt.destination_before_digest,
            "destination_after_digest": receipt.destination_after_digest,
            "record_count": receipt.record_count,
            "legacy_record_count": receipt.legacy_record_count,
            "v1_record_count": receipt.v1_record_count,
            "skipped_count": receipt.skipped_count,
            "safe_warnings": list(receipt.safe_warnings),
            "durable_state": receipt.durable_state.value,
            "owner_generation": receipt.owner_generation,
            "owner_revision": receipt.owner_revision,
            "owner_receipt_digest": receipt.owner_receipt_digest,
            "source_baseline_digest": receipt.source_baseline_digest,
        }
    )


def receipt_from_bytes(payload: bytes) -> ResourceOperationReceipt:
    value = _decode_exact_json(payload, "RESOURCE.RECEIPT.INVALID")
    keys = (
        "receipt_schema", "operation_id", "operation_kind", "resource_kind",
        "payload_profile", "source_resource_id", "destination_resource_id",
        "package_artifact_digest", "payload_digest", "destination_before_digest",
        "destination_after_digest", "record_count", "legacy_record_count",
        "v1_record_count", "skipped_count", "safe_warnings", "durable_state",
        "owner_generation", "owner_revision", "owner_receipt_digest",
        "source_baseline_digest",
    )
    _require_exact_keys_for_code(value, keys, "RESOURCE.RECEIPT.INVALID")
    try:
        warnings = value["safe_warnings"]
        if type(warnings) is not list:
            raise TypeError
        receipt = ResourceOperationReceipt(
            receipt_schema=_require_str(value["receipt_schema"]),
            operation_id=_require_str(value["operation_id"]),
            operation_kind=ResourceOperationKind(
                _require_str(value["operation_kind"])
            ),
            resource_kind=PortableResourceKind(
                _require_str(value["resource_kind"])
            ),
            payload_profile=ResourcePayloadProfile(
                _require_str(value["payload_profile"])
            ),
            source_resource_id=_require_optional_str(value["source_resource_id"]),
            destination_resource_id=_require_optional_str(
                value["destination_resource_id"]
            ),
            package_artifact_digest=_require_optional_str(
                value["package_artifact_digest"]
            ),
            payload_digest=_require_str(value["payload_digest"]),
            destination_before_digest=_require_optional_str(
                value["destination_before_digest"]
            ),
            destination_after_digest=_require_optional_str(
                value["destination_after_digest"]
            ),
            record_count=_require_exact_int(value["record_count"]),
            legacy_record_count=_require_exact_int(value["legacy_record_count"]),
            v1_record_count=_require_exact_int(value["v1_record_count"]),
            skipped_count=_require_exact_int(value["skipped_count"]),
            safe_warnings=tuple(_require_str(item) for item in warnings),
            durable_state=ResourceDurableState(
                _require_str(value["durable_state"])
            ),
            owner_generation=_require_optional_exact_int(value["owner_generation"]),
            owner_revision=_require_optional_exact_int(value["owner_revision"]),
            owner_receipt_digest=_require_optional_str(
                value["owner_receipt_digest"]
            ),
            source_baseline_digest=_require_optional_str(
                value["source_baseline_digest"]
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error
    if receipt_to_bytes(receipt) != payload:
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID")
    return receipt


def receipt_digest(receipt: ResourceOperationReceipt) -> str:
    return hashlib.sha256(receipt_to_bytes(receipt)).hexdigest()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    ).encode("utf-8")


def _decode_exact_json(payload: bytes, code: str) -> dict[str, object]:
    if type(payload) is not bytes or not payload or len(payload) > MAX_MANIFEST_BYTES:
        raise ResourcePortabilityError(code)
    duplicate = False

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        nonlocal duplicate
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                duplicate = True
            result[key] = value
        return result

    reject_constant: Callable[[str], object] = lambda _value: (_ for _ in ()).throw(
        ValueError("non-finite JSON number")
    )
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ResourcePortabilityError(code) from error
    if duplicate or type(value) is not dict or _json_depth(value) > MAX_JSON_DEPTH:
        raise ResourcePortabilityError(code)
    return value


def _json_depth(value: object) -> int:
    if type(value) is dict:
        return 1 + max((_json_depth(item) for item in value.values()), default=0)
    if type(value) is list:
        return 1 + max((_json_depth(item) for item in value), default=0)
    return 1


def _validate_member_path(value: object) -> None:
    if type(value) is not str or not value or len(value.encode("utf-8")) > MAX_MEMBER_PATH_BYTES:
        raise ValueError("RESOURCE.PACKAGE.MEMBER_INVALID")
    if "\\" in value or "\x00" in value or value.startswith("/"):
        raise ValueError("RESOURCE.PACKAGE.MEMBER_INVALID")
    segments = value.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or len(segment.encode("utf-8")) > MAX_MEMBER_SEGMENT_BYTES
        for segment in segments
    ):
        raise ValueError("RESOURCE.PACKAGE.MEMBER_INVALID")


def _require_exact_keys(value: object, keys: tuple[str, ...]) -> None:
    _require_exact_keys_for_code(
        value,
        keys,
        "RESOURCE.PACKAGE.MANIFEST_INVALID",
    )


def _require_exact_keys_for_code(
    value: object,
    keys: tuple[str, ...],
    code: str,
) -> None:
    mapping = _require_dict(value)
    if tuple(mapping) != keys:
        raise ResourcePortabilityError(code)


def _require_dict(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise TypeError("value must be exact dict")
    return value


def _require_str(value: object) -> str:
    if type(value) is not str:
        raise TypeError("value must be exact str")
    return value


def _require_optional_str(value: object) -> str | None:
    if value is None:
        return None
    return _require_str(value)


def _require_exact_int(value: object) -> int:
    if type(value) is not int:
        raise TypeError("value must be exact int")
    return value


def _require_optional_exact_int(value: object) -> int | None:
    if value is None:
        return None
    return _require_exact_int(value)


def _require_nonempty(value: object, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise TypeError(f"{name} must be a nonempty string")


def _require_optional_nonempty(value: object, name: str) -> None:
    if value is not None:
        _require_nonempty(value, name)


def _require_digest(value: object, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TypeError(f"{name} must be a lowercase SHA-256 digest")


def _require_optional_digest(value: object, name: str) -> None:
    if value is not None:
        _require_digest(value, name)


def _require_int(value: object, name: str, *, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise TypeError(f"{name} must be an exact int >= {minimum}")


def _require_optional_int(value: object, name: str) -> None:
    if value is not None:
        _require_int(value, name, minimum=0)


def _require_bool(value: object, name: str) -> None:
    if type(value) is not bool:
        raise TypeError(f"{name} must be exact bool")


def _require_safe_codes(values: object) -> None:
    if type(values) is not tuple or len(values) > MAX_SAFE_ISSUES or any(
        type(value) is not str or not value or " " in value for value in values
    ):
        raise TypeError("safe issues must be a bounded tuple of stable codes")


__all__ = [
    "CARRIER_PROFILE",
    "CARRIER_PROFILE_V2",
    "LIMIT_PROFILE",
    "LIMIT_PROFILE_V2",
    "MANIFEST_SCHEMA",
    "MANIFEST_SCHEMA_V2",
    "MAX_ARTIFACT_BYTES",
    "MAX_MANIFEST_BYTES",
    "MAX_PAYLOAD_BYTES",
    "PAYLOAD_PROFILE_SET",
    "PAYLOAD_PROFILE_SET_V2",
    "RECEIPT_SCHEMA",
    "PortableResourceKind",
    "PortableResourceSnapshot",
    "ResourceDurableState",
    "ResourceExportOutcome",
    "ResourceImportMode",
    "ResourceOperationKind",
    "ResourceOperationReceipt",
    "ResourcePackageImportPreview",
    "ResourcePackageImportResult",
    "ResourcePackageLimitProfile",
    "ResourcePackageManifest",
    "ResourcePackageSourceScope",
    "ResourceRecoveryAction",
    "ResourceRecoveryDisposition",
    "ResourceRecoveryOutcome",
    "ResourceRecoveryPreview",
    "ResourcePackageTransferMetadata",
    "ResourcePackageValidationReport",
    "ResourcePayloadDescriptor",
    "ResourcePayloadProfile",
    "ResourcePortabilityError",
    "ResourceProfileCounts",
    "manifest_from_bytes",
    "manifest_to_bytes",
    "limit_profile_for_payload",
    "package_profile_triple_for_payload",
    "payload_path_for_profile",
    "payload_profile_supports_kind",
    "profile_for_kind",
    "resource_package_capability",
    "receipt_digest",
    "receipt_from_bytes",
    "receipt_to_bytes",
]
