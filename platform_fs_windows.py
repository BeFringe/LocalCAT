"""Windows native/host fact probe for the platform file adapter.

Task 3.1 exposes native/host facts, Task 3.2 rooted read authorities, Task 3.3
persistent locks, Task 3.4 private proof, and Task 3.5 handle-bound publication.
"""

from __future__ import annotations

import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import hmac
from pathlib import Path, PurePath
import struct
import sys
import threading
import time
from typing import Callable, Iterator

from platform_fs_contracts import (
    BoundContentFacts,
    BoundDirectoryAuthority,
    BoundExistingFileMutationGuard,
    BoundRegularFile,
    BoundSynchronizedRegularFile,
    CandidateFile,
    CandidateContentFacts,
    DEVICE_SECRET_SIZE_BYTES,
    DeviceSecretAuthority,
    EntrySnapshot,
    ExactEmptyChildDirectoryRetirement,
    ExistingProcessFileLock,
    ExistingFileDurability,
    ExistingFileMutationGuard,
    ExistingFileRetirement,
    ExistingCandidateRecovery,
    ExistingRetirementSource,
    RetirementSourceDirectoryAuthority,
    FileObjectIdentity,
    LedgerEntryObservation,
    LedgerEnumerationLimits,
    LockLease,
    LockPolicy,
    LockWait,
    LockedDescendantNamespaceInspection,
    MutableFileReservation,
    MutableFileReservationService,
    OwnedNamespaceRetirement,
    PendingPublication,
    PersistentPrivateProof,
    PlatformFileError,
    PlatformFileErrorCode,
    PrivateAccessEvidence,
    PrivateProofContext,
    PrivateStorageProof,
    PublishFacts,
    PublishMode,
    ProcessFileLock,
    RootedDirectoryAuthority,
    RootedFileSystem,
    RetainedRetirement,
    RetirementDirectoryAuthority,
    VerifiedPrivateProof,
    WINDOWS_PRIVATE_PROOF_SCHEMA,
    WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
    WindowsPrivateProof,
    derive_device_key_id,
    windows_private_proof_mac_message,
)
from windows_file_api import (
    DWORD,
    FILE_BASIC_INFO,
    FILE_DISPOSITION_INFO,
    FILE_ATTRIBUTE_TAG_INFO,
    FILE_ID_INFO,
    FILE_STANDARD_INFO,
    GENERIC_MAPPING,
    LARGE_INTEGER,
    OVERLAPPED,
    ACL_SIZE_INFORMATION,
    ACE_HEADER,
    ACCESS_ALLOWED_ACE,
    SECURITY_ATTRIBUTES,
    TOKEN_USER,
    HANDLE,
    LPVOID,
    PACL,
    PRIVILEGE_SET,
    PSECURITY_DESCRIPTOR,
    PSID,
    WORD,
    Win32CallError,
    Win32Handle,
    WindowsFileAPI,
)


__all__ = [
    "WindowsHostFacts",
    "WindowsPlatformAdapter",
    "WindowsProcessFileLock",
    "WindowsRootedFileSystem",
    "probe_windows_host_facts",
]


FILE_READ_ATTRIBUTES = 0x0080
FILE_LIST_DIRECTORY = 0x0001
FILE_TRAVERSE = 0x0020
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
READ_CONTROL = 0x00020000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
CREATE_NEW = 1
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_FLAG_WRITE_THROUGH = 0x80000000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_PERSISTENT_ACLS = 0x00000008
FILE_SUPPORTS_REPARSE_POINTS = 0x00000080

FILE_BASIC_INFO_CLASS = 0
FILE_STANDARD_INFO_CLASS = 1
FILE_DISPOSITION_INFO_CLASS = 4
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_ID_INFO_CLASS = 18
FILE_ID_EXTD_DIR_INFO_CLASS = 19
VOLUME_NAME_GUID = 0x1

DRIVE_FIXED = 3
VER_NT_WORKSTATION = 1
WINDOWS_11_MINIMUM_BUILD = 22000
IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
IMAGE_FILE_MACHINE_AMD64 = 0x8664

_MAX_PATH_BUFFER = 32768
_MAX_EXTENDED_PATH_UTF16_UNITS = 32767
_READ_CHUNK_BYTES = 64 * 1024
FILE_BEGIN = 0

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_NO_MORE_FILES = 18
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_FILE_EXISTS = 80
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_ALREADY_EXISTS = 183

_FILE_ID_EXTD_DIR_HEADER = struct.Struct("<IIqqqqqqIIII16s")
_FILE_ID_EXTD_DIR_HEADER_BYTES = 88
_DIRECTORY_QUERY_BUFFER_BYTES = 64 * 1024

TOKEN_QUERY = 0x0008
TOKEN_DUPLICATE = 0x0002
TOKEN_USER_CLASS = 1
TOKEN_ELEVATION_TYPE_CLASS = 18
TOKEN_ELEVATION_TYPE_DEFAULT = 1
TOKEN_ELEVATION_TYPE_FULL = 2
TOKEN_ELEVATION_TYPE_LIMITED = 3
SECURITY_IMPERSONATION = 2
TOKEN_IMPERSONATION = 2
GENERIC_ALL = 0x10000000
FILE_GENERIC_READ = 0x00120089
FILE_GENERIC_WRITE = 0x00120116
FILE_GENERIC_EXECUTE = 0x001200A0
SDDL_REVISION_1 = 1
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
GROUP_SECURITY_INFORMATION = 0x00000002
DACL_SECURITY_INFORMATION = 0x00000004
LABEL_SECURITY_INFORMATION = 0x00000010
SE_DACL_PRESENT = 0x0004
SE_DACL_DEFAULTED = 0x0008
SE_DACL_AUTO_INHERIT_REQ = 0x0100
SE_DACL_AUTO_INHERITED = 0x0400
SE_DACL_PROTECTED = 0x1000
SE_SELF_RELATIVE = 0x8000
ACL_REVISION = 2
ACL_SIZE_INFORMATION_CLASS = 2
ACCESS_ALLOWED_ACE_TYPE = 0x00
SYSTEM_MANDATORY_LABEL_ACE_TYPE = 0x11
FILE_ALL_ACCESS = 0x001F01FF
SYSTEM_MANDATORY_LABEL_NO_WRITE_UP = 0x00000001
WIN_LOCAL_SYSTEM_SID = 22
WIN_BUILTIN_ADMINISTRATORS_SID = 26
WIN_MEDIUM_LABEL_SID = 67
SECURITY_MAX_SID_SIZE = 68
LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_LOCK_RANGE_LOW = 1
_LOCK_RANGE_HIGH = 0
_LOCK_OFFSET_LOW = 0
_LOCK_OFFSET_HIGH = 1
_LOCK_RETRY_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class WindowsHostFacts:
    """Verified read-only host/root observations for the later rooted adapter."""

    windows_build: int
    python_implementation: str
    python_major: int
    python_minor: int
    python_bits: int
    process_machine: int
    native_machine: int
    drive_type: int
    file_system: str
    volume_flags: int
    volume_serial_number: int
    root_volume_serial_number: int
    root_file_id: bytes
    root_reparse_tag: int
    maximum_component_length: int


def _capability_unavailable() -> PlatformFileError:
    return PlatformFileError(
        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
        retryable=False,
    )


def _entry_unavailable() -> PlatformFileError:
    return PlatformFileError(
        PlatformFileErrorCode.ENTRY_UNAVAILABLE,
        retryable=False,
    )


def _durability_unavailable() -> PlatformFileError:
    return PlatformFileError(
        PlatformFileErrorCode.DURABILITY_UNAVAILABLE,
        retryable=False,
    )


def _query_file_info(
    api: WindowsFileAPI,
    raw_handle: int,
    info_class: int,
    result: ctypes.Structure,
) -> None:
    api.checked_bool(
        "GetFileInformationByHandleEx",
        api.GetFileInformationByHandleEx,
        raw_handle,
        info_class,
        ctypes.byref(result),
        ctypes.sizeof(result),
    )


def _volume_facts_by_handle(
    api: WindowsFileAPI,
    raw_handle: int,
) -> tuple[int, str, int, int]:
    volume_name = ctypes.create_unicode_buffer(261)
    filesystem_name = ctypes.create_unicode_buffer(261)
    serial = DWORD()
    maximum_component = DWORD()
    flags = DWORD()
    api.checked_bool(
        "GetVolumeInformationByHandleW",
        api.GetVolumeInformationByHandleW,
        raw_handle,
        volume_name,
        len(volume_name),
        ctypes.byref(serial),
        ctypes.byref(maximum_component),
        ctypes.byref(flags),
        filesystem_name,
        len(filesystem_name),
    )
    file_system = filesystem_name.value.upper()
    maximum_component_length = int(maximum_component.value)
    if not file_system or maximum_component_length < 1:
        raise _capability_unavailable()
    return (
        int(serial.value),
        file_system,
        int(flags.value),
        maximum_component_length,
    )


def _final_path(api: WindowsFileAPI, raw_handle: int, flags: int) -> str:
    required = api.checked_length(
        "GetFinalPathNameByHandleW",
        api.GetFinalPathNameByHandleW(raw_handle, None, 0, flags),
    )
    for _ in range(4):
        if required >= _MAX_PATH_BUFFER:
            raise _capability_unavailable()
        buffer = ctypes.create_unicode_buffer(required + 1)
        copied = api.checked_length(
            "GetFinalPathNameByHandleW",
            api.GetFinalPathNameByHandleW(
                raw_handle,
                buffer,
                len(buffer),
                flags,
            ),
        )
        if copied < len(buffer):
            value = buffer.value
            if not value:
                raise _capability_unavailable()
            return value
        required = copied
    raise _capability_unavailable()


def _guid_root_from_final_path(final_path: str) -> str:
    prefix = "\\\\?\\Volume{"
    if not final_path.startswith(prefix):
        raise _capability_unavailable()
    end = final_path.find("}\\", len(prefix))
    if end < 0:
        raise _capability_unavailable()
    identifier = final_path[len(prefix) : end]
    if (
        len(identifier) != 36
        or tuple(index for index, value in enumerate(identifier) if value == "-")
        != (8, 13, 18, 23)
        or any(value not in "0123456789abcdefABCDEF-" for value in identifier)
    ):
        raise _capability_unavailable()
    return final_path[: end + 2]


def _machine_facts(api: WindowsFileAPI) -> tuple[int, int]:
    process_machine = WORD()
    native_machine = WORD()
    api.checked_bool(
        "IsWow64Process2",
        api.IsWow64Process2,
        api.GetCurrentProcess(),
        ctypes.byref(process_machine),
        ctypes.byref(native_machine),
    )
    return int(process_machine.value), int(native_machine.value)


def _fixed_drive_type(api: WindowsFileAPI, guid_root: str) -> int:
    drive_type = int(api.GetDriveTypeW(guid_root))
    if drive_type != DRIVE_FIXED:
        raise _capability_unavailable()
    return drive_type


def _validate_runtime_facts(
    *,
    windows_major: int,
    windows_build: int,
    product_type: int,
    python_implementation: str,
    python_version: tuple[int, int],
    python_bits: int,
    process_machine: int,
    native_machine: int,
) -> None:
    if (
        windows_major != 10
        or windows_build < WINDOWS_11_MINIMUM_BUILD
        or product_type != VER_NT_WORKSTATION
        or python_implementation != "cpython"
        or python_version != (3, 14)
        or python_bits != 64
        or process_machine != IMAGE_FILE_MACHINE_UNKNOWN
        or native_machine != IMAGE_FILE_MACHINE_AMD64
    ):
        raise _capability_unavailable()


def _root_handle_facts(
    api: WindowsFileAPI,
    raw_handle: int,
) -> tuple[int, bytes, int, str]:
    standard = FILE_STANDARD_INFO()
    _query_file_info(api, raw_handle, FILE_STANDARD_INFO_CLASS, standard)
    if not standard.Directory or standard.DeletePending or standard.NumberOfLinks < 1:
        raise _capability_unavailable()
    attributes = FILE_ATTRIBUTE_TAG_INFO()
    _query_file_info(api, raw_handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, attributes)
    if (
        attributes.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT
        or attributes.ReparseTag != 0
    ):
        raise _capability_unavailable()
    identity = FILE_ID_INFO()
    _query_file_info(api, raw_handle, FILE_ID_INFO_CLASS, identity)
    file_id = bytes(identity.FileId.Identifier)
    if not any(file_id):
        raise _capability_unavailable()
    final_path = _final_path(api, raw_handle, VOLUME_NAME_GUID)
    return (
        int(identity.VolumeSerialNumber),
        file_id,
        int(attributes.ReparseTag),
        _guid_root_from_final_path(final_path),
    )


def _validate_probe_root(probe_root: Path) -> None:
    if sys.platform != "win32":
        raise _capability_unavailable()
    if not isinstance(probe_root, Path) or not probe_root.is_absolute():
        raise _capability_unavailable()
    if probe_root.drive.startswith("\\") or len(probe_root.drive) != 2:
        raise _capability_unavailable()


def _probe_windows(
    probe_root: Path,
) -> WindowsHostFacts:
    _validate_probe_root(probe_root)
    version = sys.getwindowsversion()
    python_bits = struct.calcsize("P") * 8
    api = WindowsFileAPI.load()
    with api.open_handle(
        str(probe_root),
        desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
        share_mode=FILE_SHARE_READ,
        creation_disposition=OPEN_EXISTING,
        flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    ) as root_handle:
        with root_handle.borrow() as raw_root:
            root_volume_serial, root_file_id, root_reparse_tag, guid_root = (
                _root_handle_facts(api, raw_root)
            )
            process_machine, native_machine = _machine_facts(api)
            _validate_runtime_facts(
                windows_major=int(version.major),
                windows_build=int(version.build),
                product_type=int(version.product_type),
                python_implementation=sys.implementation.name,
                python_version=(int(sys.version_info.major), int(sys.version_info.minor)),
                python_bits=python_bits,
                process_machine=process_machine,
                native_machine=native_machine,
            )
            root_volume = _volume_facts_by_handle(api, raw_root)
            drive_type = _fixed_drive_type(api, guid_root)

            with api.open_handle(
                guid_root,
                desired_access=FILE_READ_ATTRIBUTES,
                share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            ) as volume_root_handle:
                with volume_root_handle.borrow() as raw_volume_root:
                    repeated_volume = _volume_facts_by_handle(api, raw_volume_root)
                    if repeated_volume != root_volume:
                        raise _capability_unavailable()
                    (
                        volume_serial,
                        file_system,
                        volume_flags,
                        maximum_component_length,
                    ) = root_volume
                    if (
                        file_system != "NTFS"
                        or not volume_flags & FILE_PERSISTENT_ACLS
                        or not volume_flags & FILE_SUPPORTS_REPARSE_POINTS
                    ):
                        raise _capability_unavailable()
                    facts = WindowsHostFacts(
                        windows_build=int(version.build),
                        python_implementation=sys.implementation.name,
                        python_major=int(sys.version_info.major),
                        python_minor=int(sys.version_info.minor),
                        python_bits=python_bits,
                        process_machine=process_machine,
                        native_machine=native_machine,
                        drive_type=drive_type,
                        file_system=file_system,
                        volume_flags=volume_flags,
                        volume_serial_number=volume_serial,
                        root_volume_serial_number=root_volume_serial,
                        root_file_id=root_file_id,
                        root_reparse_tag=root_reparse_tag,
                        maximum_component_length=maximum_component_length,
                    )
                    return facts


def probe_windows_host_facts(probe_root: Path) -> WindowsHostFacts:
    """Return validated host/root facts without minting a rooted authority."""

    try:
        facts = _probe_windows(probe_root)
        return facts
    except PlatformFileError:
        raise
    except Exception:
        raise _capability_unavailable() from None


# Task 3.2 rooted read authority. Mutation, lock, private proof, and publication
# deliberately remain unavailable until their owning tasks are implemented.
_FaultInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class _WindowsHandleProof:
    identity: FileObjectIdentity
    snapshot: EntrySnapshot
    final_path: str


@dataclass(slots=True)
class _WindowsDirectoryRecord:
    handle: object
    expected_final_path: str
    identity: FileObjectIdentity


def _platform_failure(
    code: PlatformFileErrorCode,
    *,
    retryable: bool,
) -> PlatformFileError:
    return PlatformFileError(code, retryable=retryable)


def _outside_root() -> PlatformFileError:
    return _platform_failure(PlatformFileErrorCode.OUTSIDE_ROOT, retryable=False)


def _reparse_rejected() -> PlatformFileError:
    return _platform_failure(
        PlatformFileErrorCode.REPARSE_REJECTED,
        retryable=False,
    )


def _identity_stale() -> PlatformFileError:
    return _platform_failure(PlatformFileErrorCode.IDENTITY_STALE, retryable=True)


def _publish_failed() -> PlatformFileError:
    return _platform_failure(PlatformFileErrorCode.PUBLISH_FAILED, retryable=True)


def _proof_failure(*, stale: bool) -> PlatformFileError:
    return _identity_stale() if stale else _capability_unavailable()


def _hit_fault(fault_injector: _FaultInjector | None, phase: str) -> None:
    if fault_injector is not None:
        fault_injector(phase)


def _validate_windows_component(
    component: str,
    *,
    maximum_units: int | None = None,
) -> str:
    if type(component) is not str:
        raise TypeError("Windows component must be exact str")
    if not component or component in {".", ".."}:
        raise _outside_root()
    try:
        utf16_units = len(component.encode("utf-16-le")) // 2
    except UnicodeEncodeError:
        raise _outside_root() from None
    if (
        (maximum_units is not None and utf16_units > maximum_units)
        or component[-1] in {".", " "}
        or any(ord(value) < 32 or ord(value) == 127 for value in component)
        or any(value in '<>:"/\\|?*' for value in component)
    ):
        raise _outside_root()
    stem = component.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}:
        raise _outside_root()
    if (
        len(stem) == 4
        and stem[:3] in {"COM", "LPT"}
        and stem[3] in "123456789¹²³"
    ):
        raise _outside_root()
    return component


def _validated_components(
    components: tuple[str, ...],
    *,
    maximum_units: int | None = None,
) -> tuple[str, ...]:
    for component in components:
        _validate_windows_component(component, maximum_units=maximum_units)
    return components


def _append_component(
    parent: str,
    component: str,
    *,
    maximum_units: int,
) -> str:
    checked = _validate_windows_component(component, maximum_units=maximum_units)
    result = parent.rstrip("\\") + "\\" + checked
    try:
        total_units = len(result.encode("utf-16-le")) // 2 + 1
    except UnicodeEncodeError:
        raise _outside_root() from None
    if total_units > _MAX_EXTENDED_PATH_UTF16_UNITS:
        raise _outside_root()
    return result


def _validate_directory_record_bounds(
    *,
    offset: int,
    name_length: int,
    next_offset: int,
    buffer_size: int,
) -> None:
    if _FILE_ID_EXTD_DIR_HEADER.size != _FILE_ID_EXTD_DIR_HEADER_BYTES:
        raise _identity_stale()
    record_size = _FILE_ID_EXTD_DIR_HEADER_BYTES + name_length
    if (
        name_length < 2
        or name_length % 2
        or offset < 0
        or buffer_size < 0
        or offset + record_size > buffer_size
    ):
        raise _identity_stale()
    if next_offset != 0 and (
        next_offset < record_size
        or next_offset % 8
        or offset + next_offset >= buffer_size
    ):
        raise _identity_stale()


def _identity_from_file_info(
    identity_info: FILE_ID_INFO,
    standard: FILE_STANDARD_INFO,
    *,
    kind: str,
) -> FileObjectIdentity:
    file_id = bytes(identity_info.FileId.Identifier)
    if not any(file_id) or standard.NumberOfLinks < 1:
        raise _identity_stale()
    return FileObjectIdentity(
        platform="windows",
        volume_id=int(identity_info.VolumeSerialNumber).to_bytes(8, "little"),
        file_id=file_id,
        kind=kind,
        link_count=int(standard.NumberOfLinks),
    )


def _snapshot_from_file_info(
    identity: FileObjectIdentity,
    standard: FILE_STANDARD_INFO,
    basic: FILE_BASIC_INFO,
) -> EntrySnapshot:
    return EntrySnapshot(
        identity=identity,
        byte_count=int(standard.EndOfFile),
        modified_token=struct.pack(
            "<qqI",
            int(basic.LastWriteTime.QuadPart),
            int(basic.ChangeTime.QuadPart),
            int(basic.FileAttributes),
        ),
        reparse_free=True,
    )


def _capture_handle_proof(
    api: WindowsFileAPI,
    raw_handle: int,
    *,
    expected_final_path: str | None,
    expected_kind: str | None,
    stale: bool,
    reject_wrong_kind: bool = False,
) -> _WindowsHandleProof:
    try:
        standard = FILE_STANDARD_INFO()
        _query_file_info(api, raw_handle, FILE_STANDARD_INFO_CLASS, standard)
        attributes = FILE_ATTRIBUTE_TAG_INFO()
        _query_file_info(api, raw_handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, attributes)
        basic = FILE_BASIC_INFO()
        _query_file_info(api, raw_handle, FILE_BASIC_INFO_CLASS, basic)
        identity_info = FILE_ID_INFO()
        _query_file_info(api, raw_handle, FILE_ID_INFO_CLASS, identity_info)
        final_path = _final_path(api, raw_handle, VOLUME_NAME_GUID)
    except PlatformFileError:
        raise _proof_failure(stale=stale) from None
    except Exception:
        raise _proof_failure(stale=stale) from None

    if (
        standard.DeletePending
        or standard.NumberOfLinks < 1
        or attributes.FileAttributes != basic.FileAttributes
    ):
        raise _proof_failure(stale=stale)
    if (
        attributes.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT
        or attributes.ReparseTag != 0
    ):
        raise _reparse_rejected()
    kind = "directory" if standard.Directory else "regular"
    if bool(attributes.FileAttributes & FILE_ATTRIBUTE_DIRECTORY) != bool(
        standard.Directory
    ):
        raise _proof_failure(stale=stale)
    if expected_kind is not None and kind != expected_kind:
        if reject_wrong_kind:
            raise _reparse_rejected()
        raise _proof_failure(stale=stale)
    if expected_final_path is not None and final_path != expected_final_path:
        raise _proof_failure(stale=stale) if stale else _outside_root()
    try:
        identity = _identity_from_file_info(identity_info, standard, kind=kind)
        snapshot = _snapshot_from_file_info(identity, standard, basic)
    except PlatformFileError:
        raise _proof_failure(stale=stale) from None
    except (OverflowError, ValueError):
        raise _proof_failure(stale=stale) from None
    return _WindowsHandleProof(identity, snapshot, final_path)


def _same_object(left: FileObjectIdentity, right: FileObjectIdentity) -> bool:
    return left == right


def _require_volume(
    identity: FileObjectIdentity,
    expected_volume_id: bytes,
    *,
    stale: bool,
) -> None:
    if identity.volume_id != expected_volume_id:
        raise _proof_failure(stale=stale)


def _close_handles_reverse(handles: tuple[object, ...]) -> BaseException | None:
    first_error: BaseException | None = None
    for handle in reversed(handles):
        try:
            handle.close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    return first_error


def _reprove_directory_chain(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
) -> None:
    if not records:
        raise _identity_stale()
    expected_volume_id = records[0].identity.volume_id
    for record in records:
        _require_volume(record.identity, expected_volume_id, stale=True)
        try:
            with record.handle.borrow() as raw:
                proof = _capture_handle_proof(
                    api,
                    raw,
                    expected_final_path=record.expected_final_path,
                    expected_kind="directory",
                    stale=True,
                )
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None
        if not _same_object(proof.identity, record.identity):
            raise _identity_stale()
        _require_volume(proof.identity, expected_volume_id, stale=True)


def _duplicate_directory_chain(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
) -> tuple[_WindowsDirectoryRecord, ...]:
    duplicates: list[_WindowsDirectoryRecord] = []
    try:
        for record in records:
            handle = api.duplicate_handle(record.handle)
            duplicates.append(
                _WindowsDirectoryRecord(
                    handle,
                    record.expected_final_path,
                    record.identity,
                )
            )
        result = tuple(duplicates)
        _reprove_directory_chain(api, result)
        return result
    except BaseException:
        _close_handles_reverse(tuple(record.handle for record in duplicates))
        raise


def _directory_chain_is_strict_prefix(
    ancestor: tuple[_WindowsDirectoryRecord, ...],
    descendant: tuple[_WindowsDirectoryRecord, ...],
) -> bool:
    return len(ancestor) < len(descendant) and all(
        left.expected_final_path == right.expected_final_path
        and left.identity == right.identity
        for left, right in zip(ancestor, descendant[: len(ancestor)], strict=True)
    )


def _open_directory_record(
    api: WindowsFileAPI,
    path: str,
    expected_final_path: str | None,
    *,
    stale: bool,
    expected_volume_id: bytes | None = None,
) -> _WindowsDirectoryRecord:
    handle = None
    try:
        entry_proof = None
        if expected_final_path is not None and expected_volume_id is not None:
            entry_proof = _open_entry_proof(
                api,
                path,
                expected_final_path,
                expected_kind="directory",
                expected_volume_id=expected_volume_id,
                stale=stale,
            )
            if entry_proof is None:
                raise _proof_failure(stale=stale)
        handle = api.open_handle(
            path,
            desired_access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_mode=FILE_SHARE_READ,
            creation_disposition=OPEN_EXISTING,
            flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        )
        with handle.borrow() as raw:
            proof = _capture_handle_proof(
                api,
                raw,
                expected_final_path=expected_final_path,
                expected_kind="directory",
                stale=stale,
            )
        if entry_proof is not None and not _same_object(
            entry_proof.identity,
            proof.identity,
        ):
            raise _proof_failure(stale=stale)
        return _WindowsDirectoryRecord(handle, proof.final_path, proof.identity)
    except PlatformFileError:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        raise
    except Win32CallError as error:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        del error
        raise _proof_failure(stale=stale) from None
    except BaseException:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        raise


def _open_retirement_target_record(
    api: WindowsFileAPI,
    path: str,
    expected_volume_id: bytes,
    *,
    stale: bool,
) -> _WindowsDirectoryRecord:
    """Open only the retirement leaf with rename-compatible sharing."""

    handle = None
    try:
        handle = api.open_handle(
            path,
            desired_access=FILE_TRAVERSE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            creation_disposition=OPEN_EXISTING,
            flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        )
        with handle.borrow() as raw:
            proof = _capture_handle_proof(
                api,
                raw,
                expected_final_path=path,
                expected_kind="directory",
                stale=stale,
            )
        _require_volume(proof.identity, expected_volume_id, stale=stale)
        result = _WindowsDirectoryRecord(handle, proof.final_path, proof.identity)
        handle = None
        return result
    except (TypeError, AssertionError, AttributeError):
        raise
    except PlatformFileError:
        raise
    except Win32CallError:
        raise _proof_failure(stale=stale) from None
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass


def _open_empty_child_record(
    api: WindowsFileAPI,
    path: str,
    expected_volume_id: bytes,
) -> _WindowsDirectoryRecord:
    """Retain one no-follow directory with delete-compatible sharing."""

    handle = None
    try:
        handle = api.open_handle(
            path,
            desired_access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            creation_disposition=OPEN_EXISTING,
            flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        )
        with handle.borrow() as raw:
            proof = _capture_handle_proof(
                api,
                raw,
                expected_final_path=path,
                expected_kind="directory",
                stale=True,
            )
        _require_volume(proof.identity, expected_volume_id, stale=True)
        result = _WindowsDirectoryRecord(handle, proof.final_path, proof.identity)
        handle = None
        return result
    except (TypeError, AssertionError, AttributeError):
        raise
    except PlatformFileError:
        raise
    except Win32CallError:
        raise _identity_stale() from None
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass


def _directory_handle_is_empty(api: WindowsFileAPI, raw: int) -> bool:
    """Return whether one fresh retained directory handle has no real entries."""

    while True:
        buffer = ctypes.create_string_buffer(_DIRECTORY_QUERY_BUFFER_BYTES)
        if not api.GetFileInformationByHandleEx(
            raw,
            FILE_ID_EXTD_DIR_INFO_CLASS,
            buffer,
            len(buffer),
        ):
            error = api.last_error()
            if error == ERROR_NO_MORE_FILES:
                return True
            raise Win32CallError("GetFileInformationByHandleEx", error)
        offset = 0
        while True:
            if offset + _FILE_ID_EXTD_DIR_HEADER.size > len(buffer):
                raise _identity_stale()
            values = _FILE_ID_EXTD_DIR_HEADER.unpack_from(buffer.raw, offset)
            next_offset = values[0]
            name_length = values[9]
            _validate_directory_record_bounds(
                offset=offset,
                name_length=name_length,
                next_offset=next_offset,
                buffer_size=len(buffer),
            )
            name_start = offset + _FILE_ID_EXTD_DIR_HEADER.size
            try:
                name = buffer.raw[name_start : name_start + name_length].decode(
                    "utf-16-le",
                    errors="strict",
                )
            except UnicodeDecodeError:
                raise _identity_stale() from None
            if name not in {".", ".."}:
                return False
            if next_offset == 0:
                break
            offset += next_offset


class _WindowsDirectoryRenameWindow:
    """Temporary rename-compatible replacement for one authority record."""

    __slots__ = (
        "_api",
        "_authority",
        "_index",
        "_expected_path",
        "_expected_identity",
        "_active",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        authority: object,
        index: int,
        expected_path: str,
        expected_identity: FileObjectIdentity,
    ) -> None:
        self._api = api
        self._authority = authority
        self._index = index
        self._expected_path = expected_path
        self._expected_identity = expected_identity
        self._active = True

    def restore(self) -> None:
        if not self._active:
            return
        strict = None
        try:
            records = self._authority._records
            if self._index >= len(records):
                raise _identity_stale()
            current = records[self._index]
            if (
                current.expected_final_path != self._expected_path
                or current.identity != self._expected_identity
            ):
                raise _identity_stale()
            strict = _open_directory_record(
                self._api,
                self._expected_path,
                self._expected_path,
                stale=True,
                expected_volume_id=records[0].identity.volume_id,
            )
            if strict.identity != self._expected_identity:
                raise _identity_stale()
            updated = list(records)
            updated[self._index] = strict
            self._authority._records = tuple(updated)
            strict = None
            current.handle.close()
            _reprove_directory_chain(self._api, self._authority._records)
            self._active = False
        finally:
            if strict is not None:
                try:
                    strict.handle.close()
                except BaseException:
                    pass


def _begin_directory_rename_window(
    api: WindowsFileAPI,
    authority: object,
    expected: _WindowsDirectoryRecord,
) -> _WindowsDirectoryRenameWindow:
    records = authority._records
    matches = tuple(
        index
        for index, record in enumerate(records)
        if record.expected_final_path == expected.expected_final_path
        and record.identity == expected.identity
    )
    if len(matches) != 1:
        raise _identity_stale()
    index = matches[0]
    compatible = _open_retirement_target_record(
        api,
        expected.expected_final_path,
        records[0].identity.volume_id,
        stale=True,
    )
    if compatible.identity != expected.identity:
        compatible.handle.close()
        raise _identity_stale()
    current = records[index]
    updated = list(records)
    updated[index] = compatible
    authority._records = tuple(updated)
    window = _WindowsDirectoryRenameWindow(
        api,
        authority,
        index,
        expected.expected_final_path,
        expected.identity,
    )
    try:
        current.handle.close()
        _reprove_directory_chain(api, authority._records)
    except BaseException:
        try:
            window.restore()
        except BaseException:
            pass
        raise
    return window


def _open_entry_proof(
    api: WindowsFileAPI,
    path: str,
    expected_final_path: str,
    *,
    expected_kind: str | None,
    expected_volume_id: bytes,
    stale: bool,
    allow_missing: bool = False,
    entry_unavailable: bool = False,
    reject_wrong_kind: bool = False,
) -> _WindowsHandleProof | None:
    handle = None
    try:
        handle = api.open_handle(
            path,
            desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
            share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            creation_disposition=OPEN_EXISTING,
            flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
        )
        with handle.borrow() as raw:
            proof = _capture_handle_proof(
                api,
                raw,
                expected_final_path=expected_final_path,
                expected_kind=expected_kind,
                stale=stale,
                reject_wrong_kind=reject_wrong_kind,
            )
            _require_volume(proof.identity, expected_volume_id, stale=stale)
            return proof
    except PlatformFileError:
        raise
    except Win32CallError as error:
        if allow_missing and error.winerror in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
            return None
        if entry_unavailable and error.winerror in {
            ERROR_FILE_NOT_FOUND,
            ERROR_PATH_NOT_FOUND,
            ERROR_ACCESS_DENIED,
        }:
            raise _entry_unavailable() from None
        raise _proof_failure(stale=stale) from None
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                if sys.exception() is None:
                    raise _proof_failure(stale=stale) from None


def _open_directory_components(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
    components: tuple[str, ...],
    *,
    maximum_component_units: int,
) -> tuple[_WindowsDirectoryRecord, ...]:
    expanded = list(records)
    current = expanded[-1].expected_final_path
    try:
        for component in components:
            current = _append_component(
                current,
                component,
                maximum_units=maximum_component_units,
            )
            record = _open_directory_record(
                api,
                current,
                current,
                stale=False,
                expected_volume_id=records[0].identity.volume_id,
            )
            expanded.append(record)
            _require_volume(record.identity, records[0].identity.volume_id, stale=False)
        result = tuple(expanded)
        _reprove_directory_chain(api, result)
        return result
    except BaseException:
        newly_opened = tuple(record.handle for record in expanded[len(records) :])
        _close_handles_reverse(newly_opened)
        raise


_WINDOWS_DIRECTORY_AUTHORITY_SLOTS = (
    "_api",
    "_issuer",
    "_root_anchor",
    "_records",
    "_maximum_component_units",
    "_fault_injector",
)


class _WindowsDirectoryAuthorityMixin:
    __slots__ = ()

    def __init__(
        self,
        api: WindowsFileAPI,
        issuer: object,
        root_anchor: object,
        records: tuple[_WindowsDirectoryRecord, ...],
        maximum_component_units: int,
        fault_injector: _FaultInjector | None,
    ) -> None:
        super().__init__()
        if not records:
            raise ValueError("Windows directory authority requires retained handles")
        self._api = api
        self._issuer = issuer
        self._root_anchor = root_anchor
        self._records = records
        self._maximum_component_units = maximum_component_units
        self._fault_injector = fault_injector
        try:
            _reprove_directory_chain(api, records)
        except BaseException:
            _close_handles_reverse(tuple(record.handle for record in records))
            raise

    @property
    def _leaf_path(self) -> str:
        return self._records[-1].expected_final_path

    def _reprove(self) -> None:
        _reprove_directory_chain(self._api, self._records)

    def _inspect_entry(self, name: str) -> EntrySnapshot | None:
        component = _validate_windows_component(
            name,
            maximum_units=self._maximum_component_units,
        )
        self._reprove()
        path = _append_component(
            self._leaf_path,
            component,
            maximum_units=self._maximum_component_units,
        )
        try:
            proof = _open_entry_proof(
                self._api,
                path,
                path,
                expected_kind=None,
                expected_volume_id=self._records[0].identity.volume_id,
                stale=False,
                allow_missing=True,
            )
            return None if proof is None else proof.snapshot
        except PlatformFileError:
            raise

    def _observe_ledger_entries(
        self,
        lease: LockLease,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        return self._observe_entries_under_lease_records(
            lease,
            self._api,
            self._issuer,
            self._records,
            limits,
        )

    def _observe_entries_under_lease_records(
        self,
        lease: LockLease,
        lease_api: WindowsFileAPI,
        lease_issuer: object,
        lease_records: tuple[_WindowsDirectoryRecord, ...],
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        if not isinstance(lease, _WindowsLockLease) or not lease._matches_parent(
            lease_api,
            lease_issuer,
            lease_records,
        ):
            raise _lock_unavailable()

        def observe_once() -> tuple[LedgerEntryObservation, ...]:
            record = _open_directory_record(
                self._api,
                self._leaf_path,
                self._leaf_path,
                stale=True,
                expected_volume_id=self._records[0].identity.volume_id,
            )
            try:
                if record.identity != self._records[-1].identity:
                    raise _identity_stale()
                observations: list[LedgerEntryObservation] = []
                seen_names: set[str] = set()
                total_bytes = 0
                with record.handle.borrow() as raw:
                    while True:
                        buffer = ctypes.create_string_buffer(_DIRECTORY_QUERY_BUFFER_BYTES)
                        if not self._api.GetFileInformationByHandleEx(
                            raw,
                            FILE_ID_EXTD_DIR_INFO_CLASS,
                            buffer,
                            len(buffer),
                        ):
                            error = self._api.last_error()
                            if error == ERROR_NO_MORE_FILES:
                                break
                            raise Win32CallError(
                                "GetFileInformationByHandleEx",
                                error,
                            )
                        offset = 0
                        while True:
                            if offset + _FILE_ID_EXTD_DIR_HEADER.size > len(buffer):
                                raise _identity_stale()
                            values = _FILE_ID_EXTD_DIR_HEADER.unpack_from(buffer.raw, offset)
                            (
                                next_offset,
                                _file_index,
                                _creation_time,
                                _last_access_time,
                                _last_write_time,
                                _change_time,
                                end_of_file,
                                _allocation_size,
                                attributes,
                                name_length,
                                _ea_size,
                                reparse_tag,
                                file_id,
                            ) = values
                            _validate_directory_record_bounds(
                                offset=offset,
                                name_length=name_length,
                                next_offset=next_offset,
                                buffer_size=len(buffer),
                            )
                            name_start = offset + _FILE_ID_EXTD_DIR_HEADER.size
                            try:
                                name = buffer.raw[
                                    name_start : name_start + name_length
                                ].decode("utf-16-le", errors="strict")
                            except UnicodeDecodeError:
                                raise _identity_stale() from None
                            if name not in {".", ".."}:
                                if len(observations) >= limits.maximum_entries:
                                    raise _capability_unavailable()
                                component = _validate_windows_component(
                                    name,
                                    maximum_units=self._maximum_component_units,
                                )
                                if len(component.encode("utf-8")) > limits.maximum_name_bytes:
                                    raise _capability_unavailable()
                                folded = component.casefold()
                                if folded in seen_names:
                                    raise _identity_stale()
                                seen_names.add(folded)
                                if (
                                    attributes & (
                                        FILE_ATTRIBUTE_REPARSE_POINT
                                        | FILE_ATTRIBUTE_DIRECTORY
                                    )
                                    or reparse_tag != 0
                                    or end_of_file < 0
                                    or not any(file_id)
                                ):
                                    raise _reparse_rejected()
                                entry_path = _append_component(
                                    self._leaf_path,
                                    component,
                                    maximum_units=self._maximum_component_units,
                                )
                                proof = _open_entry_proof(
                                    self._api,
                                    entry_path,
                                    entry_path,
                                    expected_kind="regular",
                                    expected_volume_id=self._records[0].identity.volume_id,
                                    stale=True,
                                )
                                if (
                                    proof is None
                                    or proof.identity.file_id != file_id
                                    or proof.identity.link_count != 1
                                    or proof.snapshot.byte_count != end_of_file
                                ):
                                    raise _identity_stale()
                                total_bytes += proof.snapshot.byte_count
                                if total_bytes > limits.maximum_total_bytes:
                                    raise _capability_unavailable()
                                observations.append(
                                    LedgerEntryObservation(component, proof.snapshot)
                                )
                            if next_offset == 0:
                                break
                            offset += next_offset
                observations.sort(key=lambda observation: observation.name)
                return tuple(observations)
            except PlatformFileError:
                raise
            except Exception:
                raise _identity_stale() from None
            finally:
                try:
                    record.handle.close()
                except BaseException:
                    if sys.exception() is None:
                        raise _identity_stale() from None

        self._reprove()
        first = observe_once()
        self._reprove()
        second = observe_once()
        self._reprove()
        if first != second:
            raise _identity_stale()
        try:
            terminal_lease_match = lease._matches_parent(
                lease_api,
                lease_issuer,
                lease_records,
            )
        except Exception:
            raise _lock_unavailable() from None
        if not terminal_lease_match:
            raise _lock_unavailable()
        return second

    def _create_candidate(self, name: str, *, private: bool) -> CandidateFile:
        component = _validate_windows_component(
            name,
            maximum_units=self._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        handle = None
        try:
            self._reprove()
            records = _duplicate_directory_chain(self._api, self._records)
            entry_path = _append_component(
                records[-1].expected_final_path,
                component,
                maximum_units=self._maximum_component_units,
            )
            user_sid = _private_primary_user_sid(self._api) if private else None
            if user_sid is None:
                handle = self._api.open_handle(
                    entry_path,
                    desired_access=(
                        GENERIC_READ
                        | GENERIC_WRITE
                        | DELETE
                        | FILE_READ_ATTRIBUTES
                        | READ_CONTROL
                        | SYNCHRONIZE
                    ),
                    share_mode=0,
                    creation_disposition=CREATE_NEW,
                    flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH,
                )
            else:
                with _private_creation_security(self._api, user_sid) as security:
                    handle = self._api.open_handle(
                        entry_path,
                        desired_access=(
                            GENERIC_READ
                            | GENERIC_WRITE
                            | DELETE
                            | FILE_READ_ATTRIBUTES
                            | READ_CONTROL
                            | SYNCHRONIZE
                        ),
                        share_mode=0,
                        creation_disposition=CREATE_NEW,
                        flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH,
                        security_attributes=security,
                    )
            with handle.borrow() as raw:
                proof = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=False,
                )
                if private:
                    security_facts = _private_security_facts(self._api, raw)
                    if security_facts.owner_sid != user_sid:
                        raise _publish_failed()
            _require_volume(
                proof.identity,
                records[0].identity.volume_id,
                stale=False,
            )
            if proof.identity.link_count != 1:
                raise _publish_failed()
            transferred_records = records
            transferred_handle = handle
            records = ()
            handle = None
            return _WindowsCandidateFile(
                self._api,
                transferred_records,
                transferred_handle,
                entry_path,
                proof.identity,
                self._maximum_component_units,
                self._fault_injector,
                private=private,
            )
        except PlatformFileError as error:
            if error.code == PlatformFileErrorCode.OUTSIDE_ROOT.value:
                raise
            raise _publish_failed() from None
        except Exception:
            raise _publish_failed() from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _rename_candidate(
        self,
        candidate: CandidateFile,
        destination: str,
        *,
        mode: PublishMode,
        lease: LockLease | None,
    ) -> PublishFacts:
        component = _validate_windows_component(
            destination,
            maximum_units=self._maximum_component_units,
        )
        if type(candidate) is not _WindowsCandidateFile:
            raise _publish_failed()
        candidate._require_publishable()
        destination_path = _append_component(
            self._leaf_path,
            component,
            maximum_units=self._maximum_component_units,
        )
        armed = False
        try:
            candidate._reprove_for_parent(self._api, self._records)
            if mode is PublishMode.REPLACE_UNDER_LOCK:
                if (
                    type(lease) is not _WindowsLockLease
                    or not lease._matches_parent(
                        self._api,
                        self._issuer,
                        self._records,
                    )
                ):
                    raise _lock_unavailable()
            elif lease is not None:
                raise _publish_failed()
            preliminary = candidate._publish_facts(mode)
            try:
                self._api.self_probe_nt_set_information_file()
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise _durability_unavailable() from None
            armed = True
            _hit_fault(self._fault_injector, "publish_before_rename")
            _rename_candidate_handle(
                self._api,
                candidate._handle,
                component,
                replace=(mode is PublishMode.REPLACE_UNDER_LOCK),
            )
            candidate._mark_named(destination_path)
            _hit_fault(self._fault_injector, "publish_after_rename")
            facts = candidate._publish_facts(mode)
            if (
                facts.content_sha256 != preliminary.content_sha256
                or facts.byte_count != preliminary.byte_count
                or facts.destination_identity != preliminary.destination_identity
            ):
                raise _recovery_required()
            _hit_fault(self._fault_injector, "publish_after_final_facts")
            return facts
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            if isinstance(error, PlatformFileError) and error.code == (
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value
            ):
                raise
            if armed:
                raise _recovery_required() from None
            if isinstance(error, PlatformFileError) and error.code in {
                PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
                PlatformFileErrorCode.LOCK_CONTENDED.value,
            }:
                raise
            raise _publish_failed() from None

    def _open_published_destination(
        self,
        destination: str,
        preliminary_facts: PublishFacts,
    ) -> BoundRegularFile:
        component = _validate_windows_component(
            destination,
            maximum_units=self._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        handle = None
        retained: _WindowsBoundRegularFile | None = None
        try:
            self._reprove()
            records = _duplicate_directory_chain(self._api, self._records)
            entry_path = _append_component(
                records[-1].expected_final_path,
                component,
                maximum_units=self._maximum_component_units,
            )
            _hit_fault(self._fault_injector, "publish_before_destination_reopen")
            probed = _open_entry_proof(
                self._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=records[0].identity.volume_id,
                stale=True,
            )
            if (
                probed is None
                or probed.identity != preliminary_facts.destination_identity
                or probed.identity.link_count != 1
            ):
                raise _recovery_required()
            handle = self._api.open_handle(
                entry_path,
                desired_access=(GENERIC_READ | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE),
                share_mode=FILE_SHARE_READ,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
            )
            with handle.borrow() as raw:
                source = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            if source.identity != probed.identity or source.identity.link_count != 1:
                raise _recovery_required()
            transferred_records = records
            transferred_handle = handle
            records = ()
            handle = None
            retained = _WindowsBoundRegularFile(
                self._api,
                transferred_records,
                transferred_handle,
                entry_path,
                source.identity,
                self._maximum_component_units,
                self._fault_injector,
            )
            _hit_fault(self._fault_injector, "publish_after_destination_reopen")
            facts = _publish_facts_from_retained(retained, preliminary_facts.mode)
            if facts != preliminary_facts:
                raise _recovery_required()
            result = retained
            retained = None
            return result
        except BaseException as error:
            if retained is not None:
                try:
                    retained.close()
                except BaseException:
                    pass
            if not isinstance(error, Exception):
                raise
            raise _recovery_required() from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _create_pending_publication(
        self,
        preliminary_facts: PublishFacts,
        retained_destination: BoundRegularFile,
    ) -> PendingPublication:
        if type(retained_destination) is not _WindowsBoundRegularFile:
            raise _recovery_required()
        return _WindowsPendingPublication(preliminary_facts, retained_destination)

    def _unlink_owned(self, name: str, expected: FileObjectIdentity) -> None:
        component = _validate_windows_component(
            name,
            maximum_units=self._maximum_component_units,
        )
        handle = None
        try:
            self._reprove()
            entry_path = _append_component(
                self._leaf_path,
                component,
                maximum_units=self._maximum_component_units,
            )
            handle = self._api.open_handle(
                entry_path,
                desired_access=DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=0,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT,
            )
            with handle.borrow() as raw:
                proof = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
                if proof.identity != expected or proof.identity.link_count != 1:
                    raise _recovery_required()
                self._reprove()
                disposition = FILE_DISPOSITION_INFO(DeleteFile=1)
                self._api.checked_bool(
                    "SetFileInformationByHandle",
                    self._api.SetFileInformationByHandle,
                    raw,
                    FILE_DISPOSITION_INFO_CLASS,
                    ctypes.byref(disposition),
                    ctypes.sizeof(disposition),
                )
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _recovery_required() from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    if sys.exception() is None:
                        raise _recovery_required() from None

    def _close_authority(self) -> None:
        error = _close_handles_reverse(tuple(record.handle for record in self._records))
        if error is not None:
            raise _capability_unavailable() from None


class _WindowsRootedDirectory(
    _WindowsDirectoryAuthorityMixin,
    RootedDirectoryAuthority,
):
    __slots__ = _WINDOWS_DIRECTORY_AUTHORITY_SLOTS


class _WindowsBoundDirectory(
    _WindowsDirectoryAuthorityMixin,
    BoundDirectoryAuthority,
):
    __slots__ = _WINDOWS_DIRECTORY_AUTHORITY_SLOTS


class _WindowsEmptyChildDirectoryAuthority(
    _WindowsDirectoryAuthorityMixin,
    BoundDirectoryAuthority,
):
    """Delete-compatible, one-operation authority for an existing child."""

    __slots__ = _WINDOWS_DIRECTORY_AUTHORITY_SLOTS


class _WindowsRetirementTargetDirectory(
    _WindowsDirectoryAuthorityMixin,
    RetirementDirectoryAuthority,
):
    __slots__ = _WINDOWS_DIRECTORY_AUTHORITY_SLOTS


def _lock_unavailable(*, retryable: bool = False) -> PlatformFileError:
    return _platform_failure(
        PlatformFileErrorCode.LOCK_UNAVAILABLE,
        retryable=retryable,
    )


def _lock_contended() -> PlatformFileError:
    return _platform_failure(PlatformFileErrorCode.LOCK_CONTENDED, retryable=True)


def _sid_bytes(api: WindowsFileAPI, sid: object) -> bytes:
    pointer = int(ctypes.cast(sid, ctypes.c_void_p).value or 0)
    if pointer == 0 or not api.IsValidSid(pointer):
        raise _lock_unavailable()
    length = int(api.GetLengthSid(pointer))
    if length < 8 or length > 68:
        raise _lock_unavailable()
    value = bytes(ctypes.string_at(pointer, length))
    if len(value) != length:
        raise _lock_unavailable()
    return value


def _sid_string(value: bytes) -> str:
    if len(value) < 8 or value[0] != 1:
        raise _lock_unavailable()
    count = value[1]
    if len(value) != 8 + count * 4:
        raise _lock_unavailable()
    authority = int.from_bytes(value[2:8], "big")
    subauthorities = [
        str(int.from_bytes(value[8 + index * 4 : 12 + index * 4], "little"))
        for index in range(count)
    ]
    suffix = "" if not subauthorities else "-" + "-".join(subauthorities)
    return f"S-1-{authority}{suffix}"


def _canonical_sid(identifier_authority: int, *subauthorities: int) -> bytes:
    if not 0 <= identifier_authority < (1 << 48) or len(subauthorities) > 15:
        raise ValueError("invalid canonical SID")
    return (
        bytes((1, len(subauthorities)))
        + identifier_authority.to_bytes(6, "big")
        + b"".join(value.to_bytes(4, "little") for value in subauthorities)
    )


_LOCAL_SYSTEM_SID = _canonical_sid(5, 18)
_BUILTIN_ADMINISTRATORS_SID = _canonical_sid(5, 32, 544)
_MEDIUM_INTEGRITY_SID = _canonical_sid(16, 8192)


def _token_user_sid(api: WindowsFileAPI, raw_token: int) -> bytes:
    needed = DWORD()
    result = api.GetTokenInformation(
        raw_token,
        TOKEN_USER_CLASS,
        None,
        0,
        ctypes.byref(needed),
    )
    if result or api.last_error() != ERROR_INSUFFICIENT_BUFFER:
        raise _lock_unavailable()
    length = int(needed.value)
    if length < ctypes.sizeof(TOKEN_USER) or length > 65536:
        raise _lock_unavailable()
    buffer = ctypes.create_string_buffer(length)
    api.checked_bool(
        "GetTokenInformation",
        api.GetTokenInformation,
        raw_token,
        TOKEN_USER_CLASS,
        buffer,
        length,
        ctypes.byref(needed),
    )
    if int(needed.value) > length:
        raise _lock_unavailable()
    token_user = ctypes.cast(buffer, ctypes.POINTER(TOKEN_USER)).contents
    return _sid_bytes(api, token_user.User.Sid)


def _current_primary_user_sid(api: WindowsFileAPI) -> bytes:
    token = _open_current_primary_token(api, TOKEN_QUERY)
    try:
        with token.borrow() as raw_token:
            return _token_user_sid(api, raw_token)
    finally:
        active = sys.exception()
        try:
            token.close()
        except BaseException:
            if active is None:
                raise


def _open_current_primary_token(api: WindowsFileAPI, access: int) -> Win32Handle:
    token_value = HANDLE()
    api.checked_bool(
        "OpenProcessToken",
        api.OpenProcessToken,
        api.GetCurrentProcess(),
        access,
        ctypes.byref(token_value),
    )
    raw_token = int(token_value.value or 0)
    if raw_token == 0:
        raise _lock_unavailable()
    return Win32Handle(
        raw_token,
        _close=api.CloseHandle,
        _last_error=api.last_error,
    )


def _well_known_sid(
    api: WindowsFileAPI,
    sid_type: int,
    expected: bytes,
) -> bytes:
    buffer = ctypes.create_string_buffer(SECURITY_MAX_SID_SIZE)
    size = DWORD(SECURITY_MAX_SID_SIZE)
    api.checked_bool(
        "CreateWellKnownSid",
        api.CreateWellKnownSid,
        sid_type,
        None,
        buffer,
        ctypes.byref(size),
    )
    if int(size.value) > SECURITY_MAX_SID_SIZE:
        raise _lock_unavailable()
    actual = _sid_bytes(api, ctypes.addressof(buffer))
    if actual != expected or len(actual) != int(size.value):
        raise _lock_unavailable()
    return actual


def _verify_one_token_access_check(
    api: WindowsFileAPI,
    descriptor: PSECURITY_DESCRIPTOR,
    token: Win32Handle,
    expected_user_sid: bytes,
) -> None:
    impersonation = None
    try:
        with token.borrow() as raw_token:
            if _token_user_sid(api, raw_token) != expected_user_sid:
                raise _lock_unavailable()
            duplicate_value = HANDLE()
            api.checked_bool(
                "DuplicateTokenEx",
                api.DuplicateTokenEx,
                raw_token,
                TOKEN_QUERY,
                None,
                SECURITY_IMPERSONATION,
                TOKEN_IMPERSONATION,
                ctypes.byref(duplicate_value),
            )
        raw_duplicate = int(duplicate_value.value or 0)
        if raw_duplicate == 0:
            raise _lock_unavailable()
        impersonation = Win32Handle(
            raw_duplicate,
            _close=api.CloseHandle,
            _last_error=api.last_error,
        )
        mapping = GENERIC_MAPPING(
            GenericRead=FILE_GENERIC_READ,
            GenericWrite=FILE_GENERIC_WRITE,
            GenericExecute=FILE_GENERIC_EXECUTE,
            GenericAll=FILE_ALL_ACCESS,
        )
        desired = DWORD(GENERIC_ALL)
        api.MapGenericMask(ctypes.byref(desired), ctypes.byref(mapping))
        if int(desired.value) != FILE_ALL_ACCESS:
            raise _lock_unavailable()
        privilege_buffer = ctypes.create_string_buffer(1024)
        privilege_length = DWORD(len(privilege_buffer))
        granted = DWORD()
        access_status = ctypes.c_int32()
        with impersonation.borrow() as raw_impersonation:
            api.checked_bool(
                "AccessCheck",
                api.AccessCheck,
                descriptor,
                raw_impersonation,
                int(desired.value),
                ctypes.byref(mapping),
                ctypes.cast(privilege_buffer, ctypes.POINTER(PRIVILEGE_SET)),
                ctypes.byref(privilege_length),
                ctypes.byref(granted),
                ctypes.byref(access_status),
            )
        if not access_status.value or int(granted.value) != FILE_ALL_ACCESS:
            raise _lock_unavailable()
    finally:
        if impersonation is not None:
            active = sys.exception()
            try:
                impersonation.close()
            except BaseException:
                if active is None:
                    raise


def _fixed_token_information(
    api: WindowsFileAPI,
    raw_token: int,
    information_class: int,
    result: ctypes._SimpleCData,
) -> None:
    returned = DWORD()
    api.checked_bool(
        "GetTokenInformation",
        api.GetTokenInformation,
        raw_token,
        information_class,
        ctypes.byref(result),
        ctypes.sizeof(result),
        ctypes.byref(returned),
    )
    if int(returned.value) != ctypes.sizeof(result):
        raise _lock_unavailable()


def _verify_primary_token_access_check(
    api: WindowsFileAPI,
    raw_handle: int,
    expected_user_sid: bytes,
) -> None:
    descriptor = PSECURITY_DESCRIPTOR()
    primary = None
    try:
        api.checked_status_zero(
            "GetSecurityInfo",
            api.GetSecurityInfo(
                raw_handle,
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION
                | GROUP_SECURITY_INFORMATION
                | DACL_SECURITY_INFORMATION,
                None,
                None,
                None,
                None,
                ctypes.byref(descriptor),
            ),
        )
        if not descriptor or not api.IsValidSecurityDescriptor(descriptor):
            raise _lock_unavailable()
        primary = _open_current_primary_token(api, TOKEN_QUERY | TOKEN_DUPLICATE)
        with primary.borrow() as raw_primary:
            elevation = DWORD()
            _fixed_token_information(
                api,
                raw_primary,
                TOKEN_ELEVATION_TYPE_CLASS,
                elevation,
            )
        if int(elevation.value) not in {
            TOKEN_ELEVATION_TYPE_DEFAULT,
            TOKEN_ELEVATION_TYPE_FULL,
            TOKEN_ELEVATION_TYPE_LIMITED,
        }:
            raise _lock_unavailable()
        _verify_one_token_access_check(api, descriptor, primary, expected_user_sid)
    finally:
        active = sys.exception()
        cleanup_error: BaseException | None = None
        if primary is not None:
            try:
                primary.close()
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if descriptor:
            try:
                api.checked_local_free(descriptor)
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if active is None and cleanup_error is not None:
            raise cleanup_error


class _LocalSecurityDescriptor:
    __slots__ = ("_api", "_pointer", "attributes")

    def __init__(self, api: WindowsFileAPI, pointer: PSECURITY_DESCRIPTOR) -> None:
        self._api = api
        self._pointer = pointer
        self.attributes = SECURITY_ATTRIBUTES(
            nLength=ctypes.sizeof(SECURITY_ATTRIBUTES),
            lpSecurityDescriptor=pointer,
            bInheritHandle=0,
        )

    def close(self) -> None:
        pointer = self._pointer
        if pointer:
            self._pointer = PSECURITY_DESCRIPTOR()
            self.attributes.lpSecurityDescriptor = None
            self._api.checked_local_free(pointer)


@contextmanager
def _protocol_control_creation_security(
    api: WindowsFileAPI,
    user_sid: bytes,
):
    descriptor = PSECURITY_DESCRIPTOR()
    descriptor_size = DWORD()
    sddl = (
        f"O:{_sid_string(user_sid)}"
        f"D:P(A;;FA;;;{_sid_string(user_sid)})(A;;FA;;;SY)(A;;FA;;;BA)"
        "S:(ML;;NW;;;ME)"
    )
    api.checked_bool(
        "ConvertStringSecurityDescriptorToSecurityDescriptorW",
        api.ConvertStringSecurityDescriptorToSecurityDescriptorW,
        sddl,
        SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    )
    if not descriptor or int(descriptor_size.value) < 20:
        if descriptor:
            api.checked_local_free(descriptor)
        raise _lock_unavailable()
    owned = _LocalSecurityDescriptor(api, descriptor)
    try:
        yield owned.attributes
    finally:
        active = sys.exception()
        try:
            owned.close()
        except BaseException:
            if active is None:
                raise


def _acl_entries(
    api: WindowsFileAPI,
    acl: PACL,
) -> tuple[tuple[int, int, int, bytes], ...]:
    address = int(ctypes.cast(acl, ctypes.c_void_p).value or 0)
    if address == 0 or not api.IsValidAcl(address):
        raise _lock_unavailable()
    header = bytes(ctypes.string_at(address, 8))
    if len(header) != 8 or header[0] != ACL_REVISION:
        raise _lock_unavailable()
    information = ACL_SIZE_INFORMATION()
    api.checked_bool(
        "GetAclInformation",
        api.GetAclInformation,
        address,
        ctypes.byref(information),
        ctypes.sizeof(information),
        ACL_SIZE_INFORMATION_CLASS,
    )
    bytes_in_use = int(information.AclBytesInUse)
    if (
        information.AceCount > 64
        or bytes_in_use < 8
        or bytes_in_use > int.from_bytes(header[2:4], "little")
    ):
        raise _lock_unavailable()
    entries: list[tuple[int, int, int, bytes]] = []
    expected_offset = 8
    for index in range(int(information.AceCount)):
        ace_pointer = LPVOID()
        api.checked_bool(
            "GetAce",
            api.GetAce,
            address,
            index,
            ctypes.byref(ace_pointer),
        )
        ace_address = int(ace_pointer.value or 0)
        if ace_address == 0 or ace_address != address + expected_offset:
            raise _lock_unavailable()
        ace_header = ctypes.cast(ace_address, ctypes.POINTER(ACE_HEADER)).contents
        ace_size = int(ace_header.AceSize)
        if (
            ace_size < ctypes.sizeof(ACCESS_ALLOWED_ACE)
            or expected_offset + ace_size > bytes_in_use
        ):
            raise _lock_unavailable()
        mask = int(
            ctypes.cast(ace_address, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents.Mask
        )
        sid = _sid_bytes(api, ace_address + ACCESS_ALLOWED_ACE.SidStart.offset)
        if ace_size != ACCESS_ALLOWED_ACE.SidStart.offset + len(sid):
            raise _lock_unavailable()
        entries.append(
            (int(ace_header.AceType), int(ace_header.AceFlags), mask, sid)
        )
        expected_offset += ace_size
    if expected_offset != bytes_in_use:
        raise _lock_unavailable()
    return tuple(entries)


def _verify_exact_security_projection(
    api: WindowsFileAPI,
    raw_handle: int,
    expected_user_sid: bytes,
) -> None:
    owner = PSID()
    dacl = PACL()
    label = PACL()
    descriptor = PSECURITY_DESCRIPTOR()
    api.checked_status_zero(
        "GetSecurityInfo",
        api.GetSecurityInfo(
            raw_handle,
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION
            | DACL_SECURITY_INFORMATION
            | LABEL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            ctypes.byref(label),
            ctypes.byref(descriptor),
        ),
    )
    if not descriptor:
        raise _lock_unavailable()
    try:
        if not api.IsValidSecurityDescriptor(descriptor):
            raise _lock_unavailable()
        control = WORD()
        revision = DWORD()
        api.checked_bool(
            "GetSecurityDescriptorControl",
            api.GetSecurityDescriptorControl,
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        )
        if int(revision.value) != SDDL_REVISION_1:
            raise _lock_unavailable()
        relevant = (
            SE_DACL_PRESENT
            | SE_DACL_DEFAULTED
            | SE_DACL_AUTO_INHERIT_REQ
            | SE_DACL_AUTO_INHERITED
            | SE_DACL_PROTECTED
        )
        if int(control.value) & relevant != SE_DACL_PRESENT | SE_DACL_PROTECTED:
            raise _lock_unavailable()

        parsed_owner = PSID()
        owner_defaulted = ctypes.c_int32()
        api.checked_bool(
            "GetSecurityDescriptorOwner",
            api.GetSecurityDescriptorOwner,
            descriptor,
            ctypes.byref(parsed_owner),
            ctypes.byref(owner_defaulted),
        )
        dacl_present = ctypes.c_int32()
        parsed_dacl = PACL()
        dacl_defaulted = ctypes.c_int32()
        api.checked_bool(
            "GetSecurityDescriptorDacl",
            api.GetSecurityDescriptorDacl,
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(parsed_dacl),
            ctypes.byref(dacl_defaulted),
        )
        label_present = ctypes.c_int32()
        parsed_label = PACL()
        label_defaulted = ctypes.c_int32()
        api.checked_bool(
            "GetSecurityDescriptorSacl",
            api.GetSecurityDescriptorSacl,
            descriptor,
            ctypes.byref(label_present),
            ctypes.byref(parsed_label),
            ctypes.byref(label_defaulted),
        )
        if (
            owner_defaulted.value
            or not dacl_present.value
            or dacl_defaulted.value
            or not label_present.value
            or label_defaulted.value
            or _sid_bytes(api, parsed_owner) != expected_user_sid
        ):
            raise _lock_unavailable()
        expected_dacl = (
            (ACCESS_ALLOWED_ACE_TYPE, 0, FILE_ALL_ACCESS, expected_user_sid),
            (
                ACCESS_ALLOWED_ACE_TYPE,
                0,
                FILE_ALL_ACCESS,
                _well_known_sid(api, WIN_LOCAL_SYSTEM_SID, _LOCAL_SYSTEM_SID),
            ),
            (
                ACCESS_ALLOWED_ACE_TYPE,
                0,
                FILE_ALL_ACCESS,
                _well_known_sid(
                    api,
                    WIN_BUILTIN_ADMINISTRATORS_SID,
                    _BUILTIN_ADMINISTRATORS_SID,
                ),
            ),
        )
        expected_label = (
            (
                SYSTEM_MANDATORY_LABEL_ACE_TYPE,
                0,
                SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
                _well_known_sid(
                    api,
                    WIN_MEDIUM_LABEL_SID,
                    _MEDIUM_INTEGRITY_SID,
                ),
            ),
        )
        if _acl_entries(api, parsed_dacl) != expected_dacl:
            raise _lock_unavailable()
        if _acl_entries(api, parsed_label) != expected_label:
            raise _lock_unavailable()
        _verify_primary_token_access_check(api, raw_handle, expected_user_sid)
    finally:
        active = sys.exception()
        try:
            api.checked_local_free(descriptor)
        except BaseException:
            if active is None:
                raise


def _seek_start(api: WindowsFileAPI, raw_handle: int) -> None:
    distance = LARGE_INTEGER()
    distance.QuadPart = 0
    api.checked_bool(
        "SetFilePointerEx",
        api.SetFilePointerEx,
        raw_handle,
        distance,
        None,
        FILE_BEGIN,
    )


def _read_exact_count(
    api: WindowsFileAPI,
    raw_handle: int,
    byte_count: int,
) -> bytes:
    if byte_count < 0 or byte_count > 1024 * 1024:
        raise _lock_unavailable()
    _seek_start(api, raw_handle)
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        requested = min(_READ_CHUNK_BYTES, remaining)
        buffer = ctypes.create_string_buffer(requested)
        read = DWORD()
        api.checked_bool(
            "ReadFile",
            api.ReadFile,
            raw_handle,
            buffer,
            requested,
            ctypes.byref(read),
            None,
        )
        count = int(read.value)
        if count < 1 or count > requested:
            raise _lock_unavailable()
        chunks.append(buffer.raw[:count])
        remaining -= count
    eof_buffer = ctypes.create_string_buffer(1)
    eof_read = DWORD()
    api.checked_bool(
        "ReadFile",
        api.ReadFile,
        raw_handle,
        eof_buffer,
        1,
        ctypes.byref(eof_read),
        None,
    )
    if int(eof_read.value) != 0:
        raise _lock_unavailable()
    return b"".join(chunks)


def _write_exact_payload(
    api: WindowsFileAPI,
    raw_handle: int,
    payload: bytes,
) -> None:
    _seek_start(api, raw_handle)
    written_total = 0
    while written_total < len(payload):
        remaining = payload[written_total:]
        buffer = ctypes.create_string_buffer(remaining)
        written = DWORD()
        api.checked_bool(
            "WriteFile",
            api.WriteFile,
            raw_handle,
            buffer,
            len(remaining),
            ctypes.byref(written),
            None,
        )
        count = int(written.value)
        if count < 1 or count > len(remaining):
            raise _lock_unavailable()
        written_total += count
    api.checked_bool("SetEndOfFile", api.SetEndOfFile, raw_handle)


def _write_stream_chunk(
    api: WindowsFileAPI,
    raw_handle: int,
    payload: bytes,
) -> None:
    written_total = 0
    while written_total < len(payload):
        remaining = payload[written_total:]
        buffer = ctypes.create_string_buffer(remaining)
        written = DWORD()
        api.checked_bool(
            "WriteFile",
            api.WriteFile,
            raw_handle,
            buffer,
            len(remaining),
            ctypes.byref(written),
            None,
        )
        count = int(written.value)
        if count < 1 or count > len(remaining):
            raise _publish_failed()
        written_total += count


def _prove_lock_handle(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
    handle: object,
    entry_path: str,
    expected_user_sid: bytes,
) -> _WindowsHandleProof:
    _reprove_directory_chain(api, records)
    try:
        with handle.borrow() as raw:
            proof = _capture_handle_proof(
                api,
                raw,
                expected_final_path=entry_path,
                expected_kind="regular",
                stale=True,
            )
            _verify_exact_security_projection(api, raw, expected_user_sid)
    except PlatformFileError:
        raise
    except Exception:
        raise _lock_unavailable() from None
    _require_volume(proof.identity, records[0].identity.volume_id, stale=True)
    if proof.identity.link_count != 1:
        raise _lock_unavailable()
    named = _open_entry_proof(
        api,
        entry_path,
        entry_path,
        expected_kind="regular",
        expected_volume_id=records[0].identity.volume_id,
        stale=True,
    )
    if named is None or named.identity != proof.identity or named.identity.link_count != 1:
        raise _lock_unavailable()
    _reprove_directory_chain(api, records)
    return proof


def _read_lock_payload(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
    handle: object,
    entry_path: str,
    expected_user_sid: bytes,
) -> tuple[bytes, _WindowsHandleProof]:
    before = _prove_lock_handle(api, records, handle, entry_path, expected_user_sid)
    try:
        with handle.borrow() as raw:
            payload = _read_exact_count(api, raw, before.snapshot.byte_count)
    except PlatformFileError:
        raise
    except Exception:
        raise _lock_unavailable() from None
    after = _prove_lock_handle(api, records, handle, entry_path, expected_user_sid)
    if before.snapshot != after.snapshot or len(payload) != after.snapshot.byte_count:
        raise _lock_unavailable()
    return payload, after


def _flush_and_readback(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
    handle: object,
    entry_path: str,
    expected_user_sid: bytes,
    expected_payload: bytes,
) -> _WindowsHandleProof:
    try:
        with handle.borrow() as raw:
            api.checked_bool("FlushFileBuffers", api.FlushFileBuffers, raw)
    except PlatformFileError:
        raise
    except Exception:
        raise _lock_unavailable() from None
    actual, proof = _read_lock_payload(
        api,
        records,
        handle,
        entry_path,
        expected_user_sid,
    )
    if actual != expected_payload:
        raise _lock_unavailable()
    return proof


def _retry_contention(policy: LockPolicy, deadline: float | None) -> None:
    if policy.wait is LockWait.FAIL_FAST:
        raise _lock_contended()
    if policy.wait is LockWait.TIMEOUT:
        if deadline is None:
            raise _lock_unavailable()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _lock_contended()
        time.sleep(min(_LOCK_RETRY_SECONDS, remaining))
        return
    time.sleep(_LOCK_RETRY_SECONDS)


def _open_protocol_handle(
    api: WindowsFileAPI,
    entry_path: str,
    *,
    creation_disposition: int,
    share_mode: int,
    security_attributes: SECURITY_ATTRIBUTES | None = None,
    init_profile: bool = False,
):
    return api.open_handle(
        entry_path,
        desired_access=(
            GENERIC_READ
            | GENERIC_WRITE
            | FILE_READ_ATTRIBUTES
            | READ_CONTROL
            | SYNCHRONIZE
        ),
        share_mode=share_mode,
        creation_disposition=creation_disposition,
        flags=(
            FILE_FLAG_OPEN_REPARSE_POINT
            | (FILE_FLAG_WRITE_THROUGH if init_profile else 0)
        ),
        security_attributes=security_attributes,
    )


def _lock_range(
    api: WindowsFileAPI,
    handle: object,
    policy: LockPolicy,
    deadline: float | None,
) -> OVERLAPPED:
    overlapped = OVERLAPPED()
    overlapped.Offset = _LOCK_OFFSET_LOW
    overlapped.OffsetHigh = _LOCK_OFFSET_HIGH
    if policy.wait is LockWait.BLOCK:
        flags = LOCKFILE_EXCLUSIVE_LOCK
        try:
            with handle.borrow() as raw:
                api.checked_bool(
                    "LockFileEx",
                    api.LockFileEx,
                    raw,
                    flags,
                    0,
                    _LOCK_RANGE_LOW,
                    _LOCK_RANGE_HIGH,
                    ctypes.byref(overlapped),
                )
            return overlapped
        except Win32CallError as error:
            if error.winerror == ERROR_LOCK_VIOLATION:
                raise _lock_contended() from None
            raise _lock_unavailable() from None
    while True:
        try:
            with handle.borrow() as raw:
                api.checked_bool(
                    "LockFileEx",
                    api.LockFileEx,
                    raw,
                    LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
                    0,
                    _LOCK_RANGE_LOW,
                    _LOCK_RANGE_HIGH,
                    ctypes.byref(overlapped),
                )
            return overlapped
        except Win32CallError as error:
            if error.winerror != ERROR_LOCK_VIOLATION:
                raise _lock_unavailable() from None
            _retry_contention(policy, deadline)


class _WindowsLockLease(LockLease):
    __slots__ = (
        "_api",
        "_issuer",
        "_root_anchor",
        "_records",
        "_handle",
        "_entry_path",
        "_entry_name",
        "_identity",
        "_user_sid",
        "_payload",
        "_overlapped",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        issuer: object,
        root_anchor: object,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object,
        entry_path: str,
        entry_name: str,
        identity: FileObjectIdentity,
        user_sid: bytes,
        payload: bytes,
        overlapped: OVERLAPPED,
    ) -> None:
        super().__init__()
        self._api = api
        self._issuer = issuer
        self._root_anchor = root_anchor
        self._records = records
        self._handle = handle
        self._entry_path = entry_path
        self._entry_name = entry_name
        self._identity = identity
        self._user_sid = user_sid
        self._payload = payload
        self._overlapped = overlapped

    def _reprove_lock(self) -> None:
        try:
            payload, proof = _read_lock_payload(
                self._api,
                self._records,
                self._handle,
                self._entry_path,
                self._user_sid,
            )
            _reprove_directory_chain(self._api, self._records)
            if proof.identity != self._identity or payload != self._payload:
                raise _lock_unavailable()
        except (TypeError, AssertionError):
            raise
        except PlatformFileError:
            raise _lock_unavailable() from None
        except Exception:
            raise _lock_unavailable() from None

    def _matches_parent(
        self,
        api: WindowsFileAPI,
        issuer: object,
        records: tuple[_WindowsDirectoryRecord, ...],
    ) -> bool:
        self._require_open()
        try:
            self._reprove_lock()
            if (
                api is not self._api
                or issuer is not self._issuer
                or len(records) != len(self._records)
            ):
                return False
            _reprove_directory_chain(api, records)
            return all(
                left.expected_final_path == right.expected_final_path
                and left.identity == right.identity
                for left, right in zip(self._records, records, strict=True)
            )
        except (TypeError, AssertionError):
            raise
        except PlatformFileError:
            raise _lock_unavailable() from None
        except Exception:
            raise _lock_unavailable() from None

    def _reprove_binding(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
    ) -> None:
        if type(parent) not in {_WindowsRootedDirectory, _WindowsBoundDirectory}:
            raise _lock_unavailable()
        if name != self._entry_name or payload != self._payload:
            raise _lock_unavailable()
        if not self._matches_parent(parent._api, parent._issuer, parent._records):
            raise _lock_unavailable()

    def _begin_directory_rename_window(
        self,
        parent: BoundDirectoryAuthority,
    ) -> _WindowsDirectoryRenameWindow:
        if (
            type(parent) not in {_WindowsRootedDirectory, _WindowsBoundDirectory}
            or parent._api is not self._api
            or parent._issuer is not self._issuer
            or parent._root_anchor is not self._root_anchor
            or not self._matches_parent(
                parent._api, parent._issuer, parent._records
            )
        ):
            raise _lock_unavailable()
        return _begin_directory_rename_window(
            self._api, self, parent._records[-1]
        )

    def _close_authority(self) -> None:
        failed = False
        primary: BaseException | None = None
        try:
            payload, proof = _read_lock_payload(
                self._api,
                self._records,
                self._handle,
                self._entry_path,
                self._user_sid,
            )
            if proof.identity != self._identity or payload != self._payload:
                failed = True
        except Exception:
            failed = True
        except BaseException as error:
            primary = error
        try:
            with self._handle.borrow() as raw:
                self._api.checked_bool(
                    "UnlockFileEx",
                    self._api.UnlockFileEx,
                    raw,
                    0,
                    _LOCK_RANGE_LOW,
                    _LOCK_RANGE_HIGH,
                    ctypes.byref(self._overlapped),
                )
        except Exception:
            failed = True
        except BaseException as error:
            if primary is None:
                primary = error
        try:
            self._handle.close()
        except Exception:
            failed = True
        except BaseException as error:
            if primary is None:
                primary = error
        chain_error = _close_handles_reverse(
            tuple(record.handle for record in self._records)
        )
        if isinstance(chain_error, Exception):
            failed = True
        elif chain_error is not None and primary is None:
            primary = chain_error
        if primary is not None:
            raise primary
        if failed:
            raise _lock_unavailable() from None


class WindowsProcessFileLock(ProcessFileLock, ExistingProcessFileLock):
    """Task 3.3 persistent W1 protocol-control LockFileEx capability."""

    def __init__(
        self,
        *,
        _fault_injector: _FaultInjector | None = None,
    ) -> None:
        if _fault_injector is not None and not callable(_fault_injector):
            raise TypeError("_fault_injector must be callable")
        self._fault_injector = _fault_injector

    def _acquire(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease:
        return self._acquire_lock(
            parent,
            name,
            payload,
            policy,
            existing_only=False,
        )

    def _acquire_existing(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
    ) -> LockLease:
        return self._acquire_lock(
            parent,
            name,
            payload,
            policy,
            existing_only=True,
        )

    def _acquire_lock(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        payload: bytes,
        policy: LockPolicy,
        *,
        existing_only: bool,
    ) -> LockLease:
        if type(existing_only) is not bool:
            raise TypeError("existing_only must be exact bool")
        if type(parent) not in {_WindowsRootedDirectory, _WindowsBoundDirectory}:
            raise _lock_unavailable()
        _validate_windows_component(
            name,
            maximum_units=parent._maximum_component_units,
        )
        if not payload or len(payload) > 1024 * 1024:
            raise _lock_unavailable()
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        handle = None
        locked = False
        overlapped = None
        deadline = (
            time.monotonic() + policy.timeout_seconds
            if policy.wait is LockWait.TIMEOUT and policy.timeout_seconds is not None
            else None
        )
        try:
            parent._reprove()
            api = parent._api
            records = _duplicate_directory_chain(api, parent._records)
            entry_path = _append_component(
                records[-1].expected_final_path,
                name,
                maximum_units=parent._maximum_component_units,
            )
            user_sid = _current_primary_user_sid(api)
            created = False
            if not existing_only:
                try:
                    with _protocol_control_creation_security(api, user_sid) as security:
                        handle = _open_protocol_handle(
                            api,
                            entry_path,
                            creation_disposition=CREATE_NEW,
                            share_mode=0,
                            security_attributes=security,
                            init_profile=True,
                        )
                    created = True
                except Win32CallError as error:
                    if error.winerror != ERROR_FILE_EXISTS:
                        raise _lock_unavailable() from None

            if created:
                _hit_fault(self._fault_injector, "lock_init_after_create")
                proof = _prove_lock_handle_without_named_open(
                    api, records, handle, entry_path, user_sid
                )
                del proof
                with handle.borrow() as raw:
                    _write_exact_payload(api, raw, payload)
                _hit_fault(self._fault_injector, "lock_init_after_write")
                with handle.borrow() as raw:
                    api.checked_bool("FlushFileBuffers", api.FlushFileBuffers, raw)
                _hit_fault(self._fault_injector, "lock_init_after_flush")
                actual = _read_lock_payload_without_named_open(
                    api, records, handle, entry_path, user_sid
                )
                if actual != payload:
                    raise _lock_unavailable()
                _hit_fault(self._fault_injector, "lock_init_after_readback")
                handle.close()
                handle = None
                _hit_fault(self._fault_injector, "lock_init_after_close")

            while True:
                try:
                    handle = _open_protocol_handle(
                        api,
                        entry_path,
                        creation_disposition=OPEN_EXISTING,
                        share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
                    )
                except Win32CallError as error:
                    if error.winerror == ERROR_SHARING_VIOLATION:
                        _retry_contention(policy, deadline)
                        continue
                    raise _lock_unavailable() from None

                actual, proof = _read_lock_payload(
                    api,
                    records,
                    handle,
                    entry_path,
                    user_sid,
                )
                if actual == payload:
                    proof = _flush_and_readback(
                        api,
                        records,
                        handle,
                        entry_path,
                        user_sid,
                        payload,
                    )
                    break
                if existing_only:
                    raise _lock_unavailable()
                if not payload.startswith(actual):
                    raise _lock_unavailable()
                handle.close()
                handle = None

                while True:
                    try:
                        handle = _open_protocol_handle(
                            api,
                            entry_path,
                            creation_disposition=OPEN_EXISTING,
                            share_mode=0,
                            init_profile=True,
                        )
                        break
                    except Win32CallError as error:
                        if error.winerror == ERROR_SHARING_VIOLATION:
                            _retry_contention(policy, deadline)
                            continue
                        raise _lock_unavailable() from None
                current = _read_lock_payload_without_named_open(
                    api,
                    records,
                    handle,
                    entry_path,
                    user_sid,
                )
                if current == payload:
                    pass
                elif payload.startswith(current):
                    with handle.borrow() as raw:
                        _write_exact_payload(api, raw, payload)
                        api.checked_bool("FlushFileBuffers", api.FlushFileBuffers, raw)
                    if (
                        _read_lock_payload_without_named_open(
                            api, records, handle, entry_path, user_sid
                        )
                        != payload
                    ):
                        raise _lock_unavailable()
                else:
                    raise _lock_unavailable()
                handle.close()
                handle = None

            overlapped = _lock_range(api, handle, policy, deadline)
            locked = True
            final_payload, final_proof = _read_lock_payload(
                api,
                records,
                handle,
                entry_path,
                user_sid,
            )
            if final_payload != payload or final_proof.identity != proof.identity:
                raise _lock_unavailable()
            transferred_records = records
            transferred_handle = handle
            records = ()
            handle = None
            locked = False
            return _WindowsLockLease(
                api,
                parent._issuer,
                parent._root_anchor,
                transferred_records,
                transferred_handle,
                entry_path,
                name,
                final_proof.identity,
                user_sid,
                payload,
                overlapped,
            )
        except PlatformFileError as error:
            if error.code in {
                PlatformFileErrorCode.LOCK_CONTENDED.value,
                PlatformFileErrorCode.LOCK_UNAVAILABLE.value,
            }:
                raise
            raise _lock_unavailable() from None
        except Exception:
            raise _lock_unavailable() from None
        finally:
            if handle is not None:
                if locked and overlapped is not None:
                    try:
                        with handle.borrow() as raw:
                            parent._api.UnlockFileEx(
                                raw,
                                0,
                                _LOCK_RANGE_LOW,
                                _LOCK_RANGE_HIGH,
                                ctypes.byref(overlapped),
                            )
                    except BaseException:
                        pass
                try:
                    handle.close()
                except BaseException:
                    pass
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))


def _prove_lock_handle_without_named_open(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
    handle: object,
    entry_path: str,
    expected_user_sid: bytes,
) -> _WindowsHandleProof:
    _reprove_directory_chain(api, records)
    try:
        with handle.borrow() as raw:
            proof = _capture_handle_proof(
                api,
                raw,
                expected_final_path=entry_path,
                expected_kind="regular",
                stale=True,
            )
            _verify_exact_security_projection(api, raw, expected_user_sid)
    except PlatformFileError:
        raise
    except Exception:
        raise _lock_unavailable() from None
    _require_volume(proof.identity, records[0].identity.volume_id, stale=True)
    if proof.identity.link_count != 1:
        raise _lock_unavailable()
    _reprove_directory_chain(api, records)
    return proof


def _read_lock_payload_without_named_open(
    api: WindowsFileAPI,
    records: tuple[_WindowsDirectoryRecord, ...],
    handle: object,
    entry_path: str,
    expected_user_sid: bytes,
) -> bytes:
    before = _prove_lock_handle_without_named_open(
        api, records, handle, entry_path, expected_user_sid
    )
    try:
        with handle.borrow() as raw:
            payload = _read_exact_count(api, raw, before.snapshot.byte_count)
    except PlatformFileError:
        raise
    except Exception:
        raise _lock_unavailable() from None
    after = _prove_lock_handle_without_named_open(
        api, records, handle, entry_path, expected_user_sid
    )
    if before.snapshot != after.snapshot or len(payload) != after.snapshot.byte_count:
        raise _lock_unavailable()
    return payload


class _WindowsBoundRegularFile(BoundRegularFile):
    __slots__ = (
        "_api",
        "_records",
        "_handle",
        "_entry_path",
        "_entry_identity",
        "_maximum_component_units",
        "_fault_injector",
        "_read_lock",
        "_content_facts_cache",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object,
        entry_path: str,
        entry_identity: FileObjectIdentity,
        maximum_component_units: int,
        fault_injector: _FaultInjector | None,
    ) -> None:
        super().__init__()
        self._api = api
        self._records = records
        self._handle = handle
        self._entry_path = entry_path
        self._entry_identity = entry_identity
        self._maximum_component_units = maximum_component_units
        self._fault_injector = fault_injector
        self._read_lock = threading.Lock()
        self._content_facts_cache: BoundContentFacts | None = None
        try:
            self._reprove()
        except BaseException:
            try:
                handle.close()
            except BaseException:
                pass
            _close_handles_reverse(tuple(record.handle for record in records))
            raise

    def _reprove(self) -> _WindowsHandleProof:
        _reprove_directory_chain(self._api, self._records)
        try:
            with self._handle.borrow() as raw:
                proof = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=self._entry_path,
                    expected_kind="regular",
                    stale=True,
                )
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None
        _require_volume(
            proof.identity,
            self._records[0].identity.volume_id,
            stale=True,
        )
        if not _same_object(proof.identity, self._entry_identity):
            raise _identity_stale()
        named = _open_entry_proof(
            self._api,
            self._entry_path,
            self._entry_path,
            expected_kind="regular",
            expected_volume_id=self._records[0].identity.volume_id,
            stale=True,
        )
        if named is None:
            raise _identity_stale()
        if not _same_object(named.identity, self._entry_identity):
            raise _identity_stale()
        return proof

    def _read_at(
        self,
        offset: int,
        maximum_bytes: int,
        expected_snapshot: EntrySnapshot,
    ) -> bytes:
        with self._read_lock:
            return self._read_at_locked(offset, maximum_bytes, expected_snapshot)

    def _read_at_locked(
        self,
        offset: int,
        maximum_bytes: int,
        expected_snapshot: EntrySnapshot,
    ) -> bytes:
        try:
            before = self._reprove().snapshot
            if before != expected_snapshot:
                raise _identity_stale()
            expected_count = min(
                maximum_bytes,
                max(0, expected_snapshot.byte_count - offset),
            )
            _hit_fault(self._fault_injector, "windows_before_body_read")
            with self._handle.borrow() as raw:
                distance = LARGE_INTEGER()
                distance.QuadPart = offset
                self._api.checked_bool(
                    "SetFilePointerEx",
                    self._api.SetFilePointerEx,
                    raw,
                    distance,
                    None,
                    FILE_BEGIN,
                )
                chunks: list[bytes] = []
                collected = 0
                if expected_count == 0:
                    eof_buffer = ctypes.create_string_buffer(1)
                    eof_read = DWORD()
                    self._api.checked_bool(
                        "ReadFile",
                        self._api.ReadFile,
                        raw,
                        eof_buffer,
                        1,
                        ctypes.byref(eof_read),
                        None,
                    )
                    if int(eof_read.value) != 0:
                        raise _identity_stale()
                while collected < expected_count:
                    requested = expected_count - collected
                    buffer = ctypes.create_string_buffer(requested)
                    read = DWORD()
                    self._api.checked_bool(
                        "ReadFile",
                        self._api.ReadFile,
                        raw,
                        buffer,
                        requested,
                        ctypes.byref(read),
                        None,
                    )
                    count = int(read.value)
                    if count < 1 or count > requested:
                        raise _identity_stale()
                    chunks.append(buffer.raw[:count])
                    collected += count
                payload = b"".join(chunks)
            _hit_fault(self._fault_injector, "windows_after_body_read")
            after = self._reprove().snapshot
            if after != expected_snapshot:
                raise _identity_stale()
            return payload
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None

    def _capture_whole_file(
        self,
        *,
        materialize: bool,
    ) -> tuple[EntrySnapshot, bytes | None, bytes]:
        """Capture one exact whole-file generation through the retained handle."""

        with self._read_lock:
            return self._capture_whole_file_locked(materialize=materialize)

    def _capture_whole_file_locked(
        self,
        *,
        materialize: bool,
    ) -> tuple[EntrySnapshot, bytes | None, bytes]:
        try:
            before = self._reprove()
            _hit_fault(self._fault_injector, "windows_before_body_read")
            with self._handle.borrow() as raw:
                _seek_start(self._api, raw)
                chunks: list[bytes] | None = [] if materialize else None
                digest = hashlib.sha256()
                remaining = before.snapshot.byte_count
                buffer = ctypes.create_string_buffer(_READ_CHUNK_BYTES)
                while remaining:
                    requested = min(_READ_CHUNK_BYTES, remaining)
                    read = DWORD()
                    self._api.checked_bool(
                        "ReadFile",
                        self._api.ReadFile,
                        raw,
                        buffer,
                        requested,
                        ctypes.byref(read),
                        None,
                    )
                    count = int(read.value)
                    if count < 1 or count > requested:
                        raise _identity_stale()
                    chunk = buffer.raw[:count]
                    digest.update(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
                    remaining -= count
                eof_buffer = ctypes.create_string_buffer(1)
                eof_read = DWORD()
                self._api.checked_bool(
                    "ReadFile",
                    self._api.ReadFile,
                    raw,
                    eof_buffer,
                    1,
                    ctypes.byref(eof_read),
                    None,
                )
                if int(eof_read.value) != 0:
                    raise _identity_stale()
            _hit_fault(self._fault_injector, "windows_after_body_read")
            after = self._reprove()
            if before.snapshot != after.snapshot:
                raise _identity_stale()
            payload = b"".join(chunks) if chunks is not None else None
            return after.snapshot, payload, digest.digest()
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise _identity_stale() from None

    def _iter_exact_chunks(
        self,
        expected_snapshot: EntrySnapshot,
    ) -> Iterator[bytes]:
        """Stream one exact generation under a single retained-handle proof window."""

        self._require_open()
        if type(expected_snapshot) is not EntrySnapshot:
            raise TypeError("expected snapshot must be exact EntrySnapshot")
        completed = False
        with self._read_lock:
            try:
                before = self._reprove()
                if before.snapshot != expected_snapshot:
                    raise _identity_stale()
                _hit_fault(self._fault_injector, "windows_before_body_read")
                with self._handle.borrow() as raw:
                    _seek_start(self._api, raw)
                    remaining = expected_snapshot.byte_count
                    buffer = ctypes.create_string_buffer(_READ_CHUNK_BYTES)
                    while remaining:
                        requested = min(_READ_CHUNK_BYTES, remaining)
                        read = DWORD()
                        self._api.checked_bool(
                            "ReadFile",
                            self._api.ReadFile,
                            raw,
                            buffer,
                            requested,
                            ctypes.byref(read),
                            None,
                        )
                        count = int(read.value)
                        if count < 1 or count > requested:
                            raise _identity_stale()
                        remaining -= count
                        yield buffer.raw[:count]
                    eof_buffer = ctypes.create_string_buffer(1)
                    eof_read = DWORD()
                    self._api.checked_bool(
                        "ReadFile",
                        self._api.ReadFile,
                        raw,
                        eof_buffer,
                        1,
                        ctypes.byref(eof_read),
                        None,
                    )
                    if int(eof_read.value) != 0:
                        raise _identity_stale()
                _hit_fault(self._fault_injector, "windows_after_body_read")
                after = self._reprove()
                if after.snapshot != expected_snapshot:
                    raise _identity_stale()
                completed = True
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise _identity_stale() from None
            finally:
                if not completed:
                    self._content_facts_cache = None

    def read_all(self) -> bytes:
        self._require_open()
        _snapshot, payload, _content_sha256 = self._capture_whole_file(
            materialize=True,
        )
        if payload is None:
            raise AssertionError("materialized whole-file capture omitted payload")
        return payload

    def _identity(self) -> FileObjectIdentity:
        return self._reprove().identity

    def _snapshot(self) -> EntrySnapshot:
        return self._reprove().snapshot

    def _content_facts(self) -> BoundContentFacts:
        with self._read_lock:
            cached = self._content_facts_cache
            if cached is not None:
                try:
                    if self._reprove().snapshot != cached.snapshot:
                        raise _identity_stale()
                except BaseException as error:
                    self._content_facts_cache = None
                    if not isinstance(error, Exception):
                        raise
                    raise _identity_stale() from None
                return cached
            snapshot, payload, content_sha256 = self._capture_whole_file_locked(
                materialize=False,
            )
            if payload is not None:
                raise AssertionError("streamed whole-file capture materialized payload")
            facts = BoundContentFacts(snapshot, content_sha256)
            self._content_facts_cache = facts
            return facts

    def _close_authority(self) -> None:
        self._content_facts_cache = None
        first_error: BaseException | None = None
        try:
            self._handle.close()
        except BaseException as error:
            first_error = error
        chain_error = _close_handles_reverse(
            tuple(record.handle for record in self._records)
        )
        if first_error is not None or chain_error is not None:
            raise _capability_unavailable() from None


class _WindowsExistingFileMutationGuard(BoundExistingFileMutationGuard):
    __slots__ = (
        "_api",
        "_issuer",
        "_root_anchor",
        "_records",
        "_handle",
        "_entry_path",
        "_entry_identity",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        issuer: object,
        root_anchor: object,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object,
        entry_path: str,
        entry_identity: FileObjectIdentity,
    ) -> None:
        super().__init__(entry_identity)
        self._api = api
        self._issuer = issuer
        self._root_anchor = root_anchor
        self._records = records
        self._handle = handle
        self._entry_path = entry_path
        self._entry_identity = entry_identity
        try:
            self._reprove_guard()
        except BaseException:
            try:
                handle.close()
            except BaseException:
                pass
            _close_handles_reverse(tuple(record.handle for record in records))
            raise

    def _reprove_guard(self) -> FileObjectIdentity:
        try:
            _reprove_directory_chain(self._api, self._records)
            with self._handle.borrow() as raw:
                retained = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=self._entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            named = _open_entry_proof(
                self._api,
                self._entry_path,
                self._entry_path,
                expected_kind="regular",
                expected_volume_id=self._records[0].identity.volume_id,
                stale=True,
            )
            if named is None or not (
                _same_object(retained.identity, self._entry_identity)
                and _same_object(named.identity, self._entry_identity)
                and retained.identity.link_count == 1
                and named.identity.link_count == 1
                and retained.snapshot == named.snapshot
            ):
                raise _identity_stale()
            _reprove_directory_chain(self._api, self._records)
            return retained.identity
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None

    def _matches_parent(
        self,
        api: WindowsFileAPI,
        issuer: object,
        records: tuple[_WindowsDirectoryRecord, ...],
    ) -> bool:
        return (
            api is self._api
            and issuer is self._issuer
            and len(records) == len(self._records)
            and all(
                left.expected_final_path == right.expected_final_path
                and left.identity == right.identity
                for left, right in zip(self._records, records, strict=True)
            )
        )

    def _begin_directory_rename_window(
        self,
        parent: BoundDirectoryAuthority,
    ) -> _WindowsDirectoryRenameWindow:
        if (
            type(parent) not in {_WindowsRootedDirectory, _WindowsBoundDirectory}
            or parent._api is not self._api
            or parent._issuer is not self._issuer
            or parent._root_anchor is not self._root_anchor
            or not self._matches_parent(
                parent._api, parent._issuer, parent._records
            )
        ):
            raise _identity_stale()
        self._reprove_guard()
        parent._reprove()
        return _begin_directory_rename_window(
            self._api, self, parent._records[-1]
        )

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        try:
            self._handle.close()
        except BaseException as error:
            first_error = error
        chain_error = _close_handles_reverse(
            tuple(record.handle for record in self._records)
        )
        if first_error is not None or chain_error is not None:
            raise _capability_unavailable() from None


class _WindowsExistingRetirementSource(ExistingRetirementSource):
    __slots__ = (
        "_api",
        "_issuer",
        "_root_anchor",
        "_records",
        "_handle",
        "_entry_path",
        "_source_name",
        "_maximum_component_units",
        "_entry_identity",
        "_expected_content",
        "_read_lock",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        issuer: object,
        root_anchor: object,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object,
        entry_path: str,
        source_name: str,
        maximum_component_units: int,
        entry_identity: FileObjectIdentity,
        expected_content: CandidateContentFacts,
    ) -> None:
        super().__init__(entry_identity, expected_content)
        self._api = api
        self._issuer = issuer
        self._root_anchor = root_anchor
        self._records = records
        self._handle = handle
        self._entry_path = entry_path
        self._source_name = source_name
        self._maximum_component_units = maximum_component_units
        self._entry_identity = entry_identity
        self._expected_content = expected_content
        self._read_lock = threading.Lock()
        try:
            self.reprove()
        except BaseException:
            self._close_authority()
            raise

    def _reprove_source(self) -> BoundContentFacts:
        with self._read_lock:
            try:
                _reprove_directory_chain(self._api, self._records)
                with self._handle.borrow() as raw:
                    before = _capture_handle_proof(
                        self._api,
                        raw,
                        expected_final_path=self._entry_path,
                        expected_kind="regular",
                        stale=True,
                    )
                named = _open_entry_proof(
                    self._api,
                    self._entry_path,
                    self._entry_path,
                    expected_kind="regular",
                    expected_volume_id=self._records[0].identity.volume_id,
                    stale=True,
                )
                if named is None or not (
                    before.identity == self._entry_identity
                    and named.identity == self._entry_identity
                    and before.identity.link_count == 1
                    and named.identity.link_count == 1
                    and before.snapshot == named.snapshot
                ):
                    raise _identity_stale()
                actual = _read_publish_facts(
                    self._api,
                    self._handle,
                    before.snapshot.byte_count,
                )
                with self._handle.borrow() as raw:
                    after = _capture_handle_proof(
                        self._api,
                        raw,
                        expected_final_path=self._entry_path,
                        expected_kind="regular",
                        stale=True,
                    )
                _reprove_directory_chain(self._api, self._records)
                if before.snapshot != after.snapshot:
                    raise _identity_stale()
                if actual != self._expected_content:
                    raise _identity_stale()
                return BoundContentFacts(after.snapshot, actual.content_sha256)
            except (TypeError, AssertionError, AttributeError):
                raise
            except PlatformFileError:
                raise
            except Exception:
                raise _identity_stale() from None

    def _transfer_live_authority(
        self,
        target_path: str,
    ) -> tuple[
        WindowsFileAPI,
        tuple[_WindowsDirectoryRecord, ...],
        object,
        str,
        FileObjectIdentity,
        CandidateContentFacts,
    ]:
        self._require_open()
        _reprove_directory_chain(self._api, self._records)
        with self._handle.borrow() as raw:
            moved = _capture_handle_proof(
                self._api,
                raw,
                expected_final_path=target_path,
                expected_kind="regular",
                stale=True,
            )
        actual = _read_publish_facts(
            self._api,
            self._handle,
            moved.snapshot.byte_count,
        )
        with self._handle.borrow() as raw:
            terminal = _capture_handle_proof(
                self._api,
                raw,
                expected_final_path=target_path,
                expected_kind="regular",
                stale=True,
            )
        if (
            moved.identity != self._entry_identity
            or moved.snapshot != terminal.snapshot
            or moved.identity.link_count != 1
            or actual != self._expected_content
        ):
            raise _recovery_required()
        records = self._records
        handle = self._handle
        self._records = ()
        self._handle = None
        self._mark_authority_transferred()
        return (
            self._api,
            records,
            handle,
            self._entry_path,
            self._entry_identity,
            self._expected_content,
        )

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except BaseException as error:
                first_error = error
        records = self._records
        self._records = ()
        chain_error = _close_handles_reverse(tuple(record.handle for record in records))
        if first_error is not None or chain_error is not None:
            raise _capability_unavailable() from None


class _WindowsRetirementSourceDirectoryAuthority(RetirementSourceDirectoryAuthority):
    __slots__ = (
        "_api",
        "_issuer",
        "_root_anchor",
        "_records",
        "_entry_path",
        "_source_name",
        "_maximum_component_units",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        issuer: object,
        root_anchor: object,
        records: tuple[_WindowsDirectoryRecord, ...],
        entry_path: str,
        source_name: str,
        maximum_component_units: int,
    ) -> None:
        super().__init__()
        self._api = api
        self._issuer = issuer
        self._root_anchor = root_anchor
        self._records = records
        self._entry_path = entry_path
        self._source_name = source_name
        self._maximum_component_units = maximum_component_units
        try:
            self._reprove_parent()
        except BaseException:
            self._close_authority()
            raise

    def _reprove_parent(self) -> None:
        _reprove_directory_chain(self._api, self._records)
        expected = _append_component(
            self._records[-1].expected_final_path,
            self._source_name,
            maximum_units=self._maximum_component_units,
        )
        if expected != self._entry_path:
            raise _identity_stale()

    def _inspect_source(self, *, stale: bool) -> EntrySnapshot | None:
        self._reprove_parent()
        proof = _open_entry_proof(
            self._api,
            self._entry_path,
            self._entry_path,
            expected_kind="regular",
            expected_volume_id=self._records[0].identity.volume_id,
            stale=stale,
            allow_missing=True,
            reject_wrong_kind=True,
        )
        return None if proof is None else proof.snapshot

    def _close_authority(self) -> None:
        error = _close_handles_reverse(
            tuple(record.handle for record in self._records)
        )
        if error is not None:
            raise _capability_unavailable() from None


class _WindowsMutableFileReservation(MutableFileReservation):
    __slots__ = (
        "_api",
        "_records",
        "_handle",
        "_entry_path",
        "_created_identity",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object,
        entry_path: str,
        created_identity: FileObjectIdentity,
    ) -> None:
        super().__init__(created_identity)
        self._api = api
        self._records = records
        self._handle = handle
        self._entry_path = entry_path
        self._created_identity = created_identity
        try:
            self._reprove_identity()
        except BaseException:
            try:
                handle.close()
            except BaseException:
                pass
            _close_handles_reverse(tuple(record.handle for record in records))
            raise

    def _reprove_identity(self) -> FileObjectIdentity:
        try:
            _reprove_directory_chain(self._api, self._records)
            with self._handle.borrow() as raw:
                retained = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=self._entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            named = _open_entry_proof(
                self._api,
                self._entry_path,
                self._entry_path,
                expected_kind="regular",
                expected_volume_id=self._records[0].identity.volume_id,
                stale=True,
            )
            if named is None or not (
                retained.identity.link_count == 1
                and named.identity.link_count == 1
                and _same_object(retained.identity, self._created_identity)
                and _same_object(named.identity, self._created_identity)
            ):
                raise _identity_stale()
            _reprove_directory_chain(self._api, self._records)
            return retained.identity
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        try:
            self._handle.close()
        except BaseException as error:
            first_error = error
        chain_error = _close_handles_reverse(
            tuple(record.handle for record in self._records)
        )
        if first_error is not None or chain_error is not None:
            raise _capability_unavailable() from None


class _WindowsRetainedRetirement(RetainedRetirement):
    __slots__ = (
        "_api",
        "_source_records",
        "_source_name",
        "_target_records",
        "_target_name",
        "_handle",
        "_target_path",
        "_expected_content",
        "_fault_injector",
    )

    def __init__(
        self,
        owned_identity: FileObjectIdentity,
        api: WindowsFileAPI,
        source_records: tuple[_WindowsDirectoryRecord, ...],
        source_name: str,
        target_records: tuple[_WindowsDirectoryRecord, ...],
        target_name: str,
        handle: object,
        target_path: str,
        expected_content: CandidateContentFacts | None = None,
        fault_injector: _FaultInjector | None = None,
    ) -> None:
        if expected_content is not None and type(expected_content) is not CandidateContentFacts:
            raise TypeError("expected_content must be exact CandidateContentFacts or None")
        super().__init__(owned_identity)
        self._api = api
        self._source_records = source_records
        self._source_name = source_name
        self._target_records = target_records
        self._target_name = target_name
        self._handle = handle
        self._target_path = target_path
        self._expected_content = expected_content
        self._fault_injector = fault_injector
        try:
            self._reprove_retirement()
        except BaseException:
            self._close_authority()
            raise

    def _reprove_retirement(self) -> EntrySnapshot:
        try:
            _reprove_directory_chain(self._api, self._source_records)
            _reprove_directory_chain(self._api, self._target_records)
            source_path = _append_component(
                self._source_records[-1].expected_final_path,
                self._source_name,
                maximum_units=_MAX_EXTENDED_PATH_UTF16_UNITS,
            )
            source = _open_entry_proof(
                self._api,
                source_path,
                source_path,
                expected_kind=None,
                expected_volume_id=self._source_records[0].identity.volume_id,
                stale=True,
                allow_missing=True,
            )
            if source is not None:
                raise _recovery_required()
            with self._handle.borrow() as raw:
                retained = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=self._target_path,
                    expected_kind="regular",
                    stale=True,
                )
            named = _open_entry_proof(
                self._api,
                self._target_path,
                self._target_path,
                expected_kind="regular",
                expected_volume_id=self._target_records[0].identity.volume_id,
                stale=True,
            )
            if (
                named is None
                or retained.identity.link_count != 1
                or named.identity.link_count != 1
                or retained.identity != named.identity
            ):
                raise _recovery_required()
            terminal_source = _open_entry_proof(
                self._api,
                source_path,
                source_path,
                expected_kind=None,
                expected_volume_id=self._source_records[0].identity.volume_id,
                stale=True,
                allow_missing=True,
            )
            terminal_target = _open_entry_proof(
                self._api,
                self._target_path,
                self._target_path,
                expected_kind="regular",
                expected_volume_id=self._target_records[0].identity.volume_id,
                stale=True,
            )
            with self._handle.borrow() as raw:
                terminal_retained = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=self._target_path,
                    expected_kind="regular",
                    stale=True,
                )
            if (
                terminal_source is not None
                or terminal_target is None
                or terminal_target.identity != retained.identity
                or terminal_retained.identity != retained.identity
                or terminal_target.identity.link_count != 1
                or terminal_retained.identity.link_count != 1
            ):
                raise _recovery_required()
            if self._expected_content is not None:
                actual = _read_publish_facts(
                    self._api,
                    self._handle,
                    terminal_retained.snapshot.byte_count,
                )
                with self._handle.borrow() as raw:
                    after_content = _capture_handle_proof(
                        self._api,
                        raw,
                        expected_final_path=self._target_path,
                        expected_kind="regular",
                        stale=True,
                    )
                if (
                    actual != self._expected_content
                    or after_content.snapshot != terminal_retained.snapshot
                ):
                    raise _recovery_required()
            _hit_fault(
                self._fault_injector,
                "existing_retirement_before_terminal_reproof",
            )
            _reprove_directory_chain(self._api, self._source_records)
            _reprove_directory_chain(self._api, self._target_records)
            return terminal_retained.snapshot
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise _recovery_required() from None
        except Exception:
            raise _recovery_required() from None

    def _close_retirement_authority(self) -> None:
        first_error: BaseException | None = None
        try:
            self._handle.close()
        except BaseException as error:
            first_error = error
        source_error = _close_handles_reverse(
            tuple(record.handle for record in self._source_records)
        )
        target_error = _close_handles_reverse(
            tuple(record.handle for record in self._target_records)
        )
        if first_error is not None or source_error is not None or target_error is not None:
            raise _capability_unavailable() from None


class _WindowsBoundSynchronizedRegularFile(
    _WindowsBoundRegularFile,
    BoundSynchronizedRegularFile,
):
    __slots__ = ()

    def _synchronize_content(
        self,
        expected: BoundContentFacts,
    ) -> BoundContentFacts:
        try:
            self._reprove()
            with self._handle.borrow() as raw:
                self._api.checked_bool(
                    "FlushFileBuffers",
                    self._api.FlushFileBuffers,
                    raw,
                )
            self._reprove()
            return expected
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            if isinstance(error, PlatformFileError) and error.code == (
                PlatformFileErrorCode.IDENTITY_STALE.value
            ):
                raise
            raise _durability_unavailable() from None


def _read_publish_facts(
    api: WindowsFileAPI,
    handle: object,
    byte_count: int,
) -> CandidateContentFacts:
    if type(byte_count) is not int or byte_count < 0:
        raise _publish_failed()
    with handle.borrow() as raw:
        _seek_start(api, raw)
        digest = hashlib.sha256()
        consumed = 0
        remaining = byte_count
        while remaining:
            requested = min(_READ_CHUNK_BYTES, remaining)
            buffer = ctypes.create_string_buffer(requested)
            read = DWORD()
            api.checked_bool(
                "ReadFile",
                api.ReadFile,
                raw,
                buffer,
                requested,
                ctypes.byref(read),
                None,
            )
            count = int(read.value)
            if count < 1 or count > requested:
                raise _publish_failed()
            digest.update(buffer.raw[:count])
            consumed += count
            remaining -= count
        eof_buffer = ctypes.create_string_buffer(1)
        eof_read = DWORD()
        api.checked_bool(
            "ReadFile",
            api.ReadFile,
            raw,
            eof_buffer,
            1,
            ctypes.byref(eof_read),
            None,
        )
        if int(eof_read.value) != 0:
            raise _publish_failed()
    return CandidateContentFacts(consumed, digest.digest())


def _rename_candidate_handle(
    api: WindowsFileAPI,
    candidate_handle: object,
    destination_component: str,
    *,
    replace: bool,
) -> None:
    with candidate_handle.borrow() as raw_candidate:
        api.rename_file_same_parent(
            raw_candidate,
            destination_component,
            replace_if_exists=replace,
        )


class _WindowsCandidateFile(CandidateFile):
    _MAXIMUM_NATIVE_WRITE_BATCH_BYTES = 1024 * 1024

    __slots__ = (
        "_api",
        "_records",
        "_handle",
        "_entry_path",
        "_entry_identity",
        "_maximum_component_units",
        "_fault_injector",
        "_private",
        "_expected_digest",
        "_expected_count",
        "_flushed_digest",
        "_flushed_count",
        "_named",
    )

    def __init__(
        self,
        api: WindowsFileAPI,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object,
        entry_path: str,
        entry_identity: FileObjectIdentity,
        maximum_component_units: int,
        fault_injector: _FaultInjector | None,
        *,
        private: bool,
        recovered_content: CandidateContentFacts | None = None,
    ) -> None:
        super().__init__(_recovered_content=recovered_content)
        self._api = api
        self._records = records
        self._handle = handle
        self._entry_path = entry_path
        self._entry_identity = entry_identity
        self._maximum_component_units = maximum_component_units
        self._fault_injector = fault_injector
        self._private = private
        self._expected_digest = (
            None if recovered_content is None else recovered_content.content_sha256
        )
        self._expected_count = (
            None if recovered_content is None else recovered_content.byte_count
        )
        self._flushed_digest = (
            None if recovered_content is None else recovered_content.content_sha256
        )
        self._flushed_count = (
            None if recovered_content is None else recovered_content.byte_count
        )
        self._named = False
        try:
            self._reprove_handle()
        except BaseException:
            try:
                handle.close()
            except BaseException:
                pass
            _close_handles_reverse(tuple(record.handle for record in records))
            raise

    def _failure(self) -> PlatformFileError:
        return _recovery_required() if self._named else _publish_failed()

    def _reprove_handle(self) -> _WindowsHandleProof:
        try:
            _reprove_directory_chain(self._api, self._records)
            with self._handle.borrow() as raw:
                proof = _capture_handle_proof(
                    self._api,
                    raw,
                    expected_final_path=self._entry_path,
                    expected_kind="regular",
                    stale=True,
                )
                if self._private:
                    _private_security_facts(self._api, raw)
            if (
                proof.identity != self._entry_identity
                or proof.identity.volume_id != self._records[0].identity.volume_id
                or proof.identity.link_count != 1
            ):
                raise self._failure()
            _reprove_directory_chain(self._api, self._records)
            return proof
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise self._failure() from None

    def _reprove_for_parent(
        self,
        api: WindowsFileAPI,
        records: tuple[_WindowsDirectoryRecord, ...],
    ) -> None:
        self._require_open()
        if api is not self._api or len(records) != len(self._records):
            raise _publish_failed()
        try:
            self._reprove_handle()
            _reprove_directory_chain(api, records)
            if not all(
                left.expected_final_path == right.expected_final_path
                and left.identity == right.identity
                for left, right in zip(self._records, records, strict=True)
            ):
                raise _publish_failed()
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise self._failure() from None

    def _write_chunks(self, chunks: Iterator[bytes]) -> CandidateContentFacts:
        try:
            self._reprove_handle()
            digest = hashlib.sha256()
            byte_count = 0
            with self._handle.borrow() as raw:
                _seek_start(self._api, raw)
                self._api.checked_bool("SetEndOfFile", self._api.SetEndOfFile, raw)
                pending: list[bytes] = []
                pending_count = 0

                def write_pending() -> None:
                    nonlocal pending_count
                    if not pending:
                        return
                    _write_stream_chunk(self._api, raw, b"".join(pending))
                    pending.clear()
                    pending_count = 0

                for chunk in chunks:
                    if (
                        pending_count + len(chunk)
                        > self._MAXIMUM_NATIVE_WRITE_BATCH_BYTES
                    ):
                        write_pending()
                    pending.append(chunk)
                    pending_count += len(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
                    if pending_count == self._MAXIMUM_NATIVE_WRITE_BATCH_BYTES:
                        write_pending()
                write_pending()
                self._api.checked_bool("SetEndOfFile", self._api.SetEndOfFile, raw)
            proof = self._reprove_handle()
            if proof.snapshot.byte_count != byte_count:
                raise self._failure()
            self._expected_digest = digest.digest()
            self._expected_count = byte_count
            self._flushed_digest = None
            self._flushed_count = None
            return CandidateContentFacts(byte_count, digest.digest())
        except (TypeError, ValueError):
            # Contract validation is performed lazily while the backend
            # consumes the checked iterator. Preserve those exact public
            # errors instead of misclassifying caller input as an OS publish
            # failure; the POSIX adapter has the same boundary.
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            raise self._failure() from None

    def _flush_content(self, expected: CandidateContentFacts) -> CandidateContentFacts:
        if self._expected_digest is None or self._expected_count is None:
            raise _publish_failed()
        if expected != CandidateContentFacts(self._expected_count, self._expected_digest):
            raise _publish_failed()
        try:
            before = self._reprove_handle()
            if before.snapshot.byte_count != self._expected_count:
                raise self._failure()
            try:
                with self._handle.borrow() as raw:
                    self._api.checked_bool(
                        "FlushFileBuffers",
                        self._api.FlushFileBuffers,
                        raw,
                    )
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise _durability_unavailable() from None
            actual = _read_publish_facts(
                self._api,
                self._handle,
                self._expected_count,
            )
            after = self._reprove_handle()
            if (
                before.snapshot != after.snapshot
                or actual != expected
            ):
                raise self._failure()
            self._flushed_digest = actual.content_sha256
            self._flushed_count = actual.byte_count
            return actual
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            if isinstance(error, PlatformFileError) and error.code == (
                PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value
            ):
                raise
            raise self._failure() from None

    def _identity(self) -> FileObjectIdentity:
        return self._reprove_handle().identity

    def _publish_facts(self, mode: PublishMode) -> PublishFacts:
        if self._flushed_digest is None or self._flushed_count is None:
            raise self._failure()
        proof = self._reprove_handle()
        actual = _read_publish_facts(self._api, self._handle, self._flushed_count)
        after = self._reprove_handle()
        if (
            proof.snapshot != after.snapshot
            or actual.byte_count != self._flushed_count
            or actual.content_sha256 != self._flushed_digest
        ):
            raise self._failure()
        return PublishFacts(
            mode=mode,
            destination_identity=after.identity,
            content_sha256=actual.content_sha256,
            byte_count=actual.byte_count,
            reparse_free=True,
        )

    def _mark_named(self, entry_path: str) -> None:
        self._entry_path = entry_path
        self._named = True

    def _close_authority(self) -> None:
        first_error: BaseException | None = None
        try:
            self._handle.close()
        except BaseException as error:
            first_error = error
        chain_error = _close_handles_reverse(
            tuple(record.handle for record in self._records)
        )
        error = first_error if first_error is not None else chain_error
        if error is not None:
            if not isinstance(error, Exception):
                raise error
            raise self._failure() from None


def _publish_facts_from_retained(
    retained: _WindowsBoundRegularFile,
    mode: PublishMode,
) -> PublishFacts:
    try:
        facts = retained.content_facts()
        return PublishFacts(
            mode=mode,
            destination_identity=facts.snapshot.identity,
            content_sha256=facts.content_sha256,
            byte_count=facts.snapshot.byte_count,
            reparse_free=True,
        )
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise _recovery_required() from None


class _WindowsPendingPublication(PendingPublication):
    __slots__ = ()

    def _terminal_reproof(
        self,
        retained_destination: BoundRegularFile,
        preliminary_facts: PublishFacts,
    ) -> PublishFacts:
        if type(retained_destination) is not _WindowsBoundRegularFile:
            raise _recovery_required()
        facts = _publish_facts_from_retained(retained_destination, preliminary_facts.mode)
        if facts != preliminary_facts:
            raise _recovery_required()
        return facts


class WindowsRootedFileSystem(
    RootedFileSystem,
    MutableFileReservationService,
    OwnedNamespaceRetirement,
    ExistingFileDurability,
    ExistingFileMutationGuard,
    LockedDescendantNamespaceInspection,
    ExistingFileRetirement,
    ExistingCandidateRecovery,
    ExactEmptyChildDirectoryRetirement,
):
    """Windows rooted reads, mutable reservations, and existing-file durability."""

    def __init__(
        self,
        *,
        _api: WindowsFileAPI | None = None,
        _fault_injector: _FaultInjector | None = None,
    ) -> None:
        if _api is not None and not isinstance(_api, WindowsFileAPI):
            raise TypeError("_api must be WindowsFileAPI")
        if _fault_injector is not None and not callable(_fault_injector):
            raise TypeError("_fault_injector must be callable")
        self._api = _api
        self._authority_issuer = object()
        self._fault_injector = _fault_injector

    def _bind_existing_child_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority:
        if type(parent) not in {
            _WindowsRootedDirectory,
            _WindowsBoundDirectory,
            _WindowsEmptyChildDirectoryAuthority,
        } or parent._issuer is not self._authority_issuer:
            raise _capability_unavailable()
        component = _validate_windows_component(
            name,
            maximum_units=parent._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        child_record = None
        try:
            parent._reprove()
            records = _duplicate_directory_chain(parent._api, parent._records)
            child_path = _append_component(
                parent._leaf_path,
                component,
                maximum_units=parent._maximum_component_units,
            )
            child_record = _open_empty_child_record(
                parent._api,
                child_path,
                records[0].identity.volume_id,
            )
            expanded = records + (child_record,)
            child_record = None
            records = ()
            return _WindowsEmptyChildDirectoryAuthority(
                parent._api,
                parent._issuer,
                parent._root_anchor,
                expanded,
                parent._maximum_component_units,
                self._fault_injector,
            )
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None
        finally:
            if child_record is not None:
                try:
                    child_record.handle.close()
                except BaseException:
                    pass
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _remove_empty_owned_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
        child: BoundDirectoryAuthority,
    ) -> None:
        if (
            type(parent)
            not in {
                _WindowsRootedDirectory,
                _WindowsBoundDirectory,
                _WindowsEmptyChildDirectoryAuthority,
            }
            or type(child) is not _WindowsEmptyChildDirectoryAuthority
            or parent._issuer is not self._authority_issuer
            or parent._api is not child._api
            or parent._issuer is not child._issuer
            or parent._root_anchor is not child._root_anchor
            or len(child._records) != len(parent._records) + 1
            or any(
                left.expected_final_path != right.expected_final_path
                or left.identity != right.identity
                for left, right in zip(
                    parent._records,
                    child._records[:-1],
                    strict=True,
                )
            )
        ):
            raise _recovery_required()
        component = _validate_windows_component(
            name,
            maximum_units=parent._maximum_component_units,
        )
        expected_path = _append_component(
            parent._leaf_path,
            component,
            maximum_units=parent._maximum_component_units,
        )
        expected = child._records[-1]
        if expected.expected_final_path != expected_path:
            raise _recovery_required()
        delete_handle = None
        armed = False
        try:
            parent._reprove()
            child._reprove()
            with expected.handle.borrow() as raw_child:
                if not _directory_handle_is_empty(child._api, raw_child):
                    raise _recovery_required()
            named = _open_entry_proof(
                parent._api,
                expected_path,
                expected_path,
                expected_kind="directory",
                expected_volume_id=parent._records[0].identity.volume_id,
                stale=True,
            )
            if named is None or named.identity != expected.identity:
                raise _recovery_required()
            delete_handle = parent._api.open_handle(
                expected_path,
                desired_access=DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
            )
            with delete_handle.borrow() as raw_delete:
                deleting = _capture_handle_proof(
                    parent._api,
                    raw_delete,
                    expected_final_path=expected_path,
                    expected_kind="directory",
                    stale=True,
                )
                if deleting.identity != expected.identity:
                    raise _recovery_required()
                parent._reprove()
                named = _open_entry_proof(
                    parent._api,
                    expected_path,
                    expected_path,
                    expected_kind="directory",
                    expected_volume_id=parent._records[0].identity.volume_id,
                    stale=True,
                )
                if named is None or named.identity != expected.identity:
                    raise _recovery_required()
                disposition = FILE_DISPOSITION_INFO(DeleteFile=1)
                parent._api.checked_bool(
                    "SetFileInformationByHandle",
                    parent._api.SetFileInformationByHandle,
                    raw_delete,
                    FILE_DISPOSITION_INFO_CLASS,
                    ctypes.byref(disposition),
                    ctypes.sizeof(disposition),
                )
                armed = True
            child.close()
            delete_handle.close()
            delete_handle = None
            parent._reprove()
            if parent._inspect_entry(component) is not None:
                raise _recovery_required()
            parent._reprove()
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise _recovery_required() from None
        except Exception:
            raise _recovery_required() from None
        finally:
            if delete_handle is not None:
                try:
                    delete_handle.close()
                except BaseException:
                    if armed and sys.exception() is None:
                        raise _recovery_required() from None
    def _native_api(self) -> WindowsFileAPI:
        if self._api is None:
            self._api = WindowsFileAPI.load()
        return self._api

    def _observe_descendant_entries(
        self,
        root: RootedDirectoryAuthority,
        lease: LockLease,
        descendant: BoundDirectoryAuthority,
        limits: LedgerEnumerationLimits,
    ) -> tuple[LedgerEntryObservation, ...]:
        if (
            type(root) is not _WindowsRootedDirectory
            or type(descendant) is not _WindowsBoundDirectory
            or not isinstance(lease, _WindowsLockLease)
            or root._api is not descendant._api
            or root._api is not lease._api
            or root._issuer is not self._authority_issuer
            or descendant._issuer is not self._authority_issuer
            or not _directory_chain_is_strict_prefix(root._records, descendant._records)
        ):
            raise _lock_unavailable()
        try:
            root._reprove()
            descendant._reprove()
            if not lease._matches_parent(
                root._api,
                root._issuer,
                root._records,
            ):
                raise _lock_unavailable()
            observations = descendant._observe_entries_under_lease_records(
                lease,
                root._api,
                root._issuer,
                root._records,
                limits,
            )
            root._reprove()
            descendant._reprove()
            if (
                not _directory_chain_is_strict_prefix(
                    root._records,
                    descendant._records,
                )
                or not lease._matches_parent(
                    root._api,
                    root._issuer,
                    root._records,
                )
            ):
                raise _lock_unavailable()
            return observations
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise
        except Exception:
            raise _lock_unavailable() from None

    def _bind_retirement_source_directory(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> RetirementSourceDirectoryAuthority:
        if type(root) is not _WindowsRootedDirectory:
            raise _capability_unavailable()
        if root._issuer is not self._authority_issuer:
            raise _capability_unavailable()
        components = _validated_components(
            tuple(relative.parts),
            maximum_units=root._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] | None = None
        try:
            root._reprove()
            records = _duplicate_directory_chain(root._api, root._records)
            parent_components = components[:-1]
            if len(parent_components) > 1:
                records = _open_directory_components(
                    root._api,
                    records,
                    parent_components[:-1],
                    maximum_component_units=root._maximum_component_units,
                )
            if parent_components:
                parent_path = _append_component(
                    records[-1].expected_final_path,
                    parent_components[-1],
                    maximum_units=root._maximum_component_units,
                )
                compatible_leaf = _open_retirement_target_record(
                    root._api,
                    parent_path,
                    records[0].identity.volume_id,
                    stale=False,
                )
                records = records + (compatible_leaf,)
            entry_path = _append_component(
                records[-1].expected_final_path,
                components[-1],
                maximum_units=root._maximum_component_units,
            )
            transferred_records = records
            records = None
            return _WindowsRetirementSourceDirectoryAuthority(
                root._api,
                root._issuer,
                root._root_anchor,
                transferred_records,
                entry_path,
                components[-1],
                root._maximum_component_units,
            )
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise
        except Exception:
            raise _capability_unavailable() from None
        finally:
            if records is not None:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _open_existing_retirement_source(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        expected_content: CandidateContentFacts,
    ) -> ExistingRetirementSource:
        if (
            type(source_parent) is not _WindowsRetirementSourceDirectoryAuthority
            or source_parent._issuer is not self._authority_issuer
        ):
            raise _capability_unavailable()
        records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        try:
            source_parent._reprove_parent()
            records = _duplicate_directory_chain(
                source_parent._api,
                source_parent._records,
            )
            entry_path = source_parent._entry_path
            probed = _open_entry_proof(
                source_parent._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=source_parent._records[0].identity.volume_id,
                stale=False,
                entry_unavailable=True,
                reject_wrong_kind=True,
            )
            if probed is None:
                raise _entry_unavailable()
            if probed.identity.link_count != 1:
                raise _identity_stale()
            handle = source_parent._api.open_handle(
                entry_path,
                desired_access=(
                    GENERIC_READ | DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE
                ),
                share_mode=FILE_SHARE_READ,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
            )
            with handle.borrow() as raw:
                opened = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            actual = _read_publish_facts(
                source_parent._api,
                handle,
                opened.snapshot.byte_count,
            )
            with handle.borrow() as raw:
                terminal = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            named = _open_entry_proof(
                source_parent._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=records[0].identity.volume_id,
                stale=True,
            )
            source_parent._reprove_parent()
            if (
                named is None
                or probed.snapshot != opened.snapshot
                or opened.snapshot != terminal.snapshot
                or terminal.snapshot != named.snapshot
                or terminal.identity.link_count != 1
                or actual != expected_content
            ):
                raise _identity_stale()
            transferred_records = records
            transferred_handle = handle
            records = None
            handle = None
            return _WindowsExistingRetirementSource(
                source_parent._api,
                source_parent._issuer,
                source_parent._root_anchor,
                transferred_records,
                transferred_handle,
                entry_path,
                source_parent._source_name,
                source_parent._maximum_component_units,
                terminal.identity,
                expected_content,
            )
        except (TypeError, AssertionError, AttributeError):
            raise
        except Win32CallError as error:
            if error.winerror == ERROR_SHARING_VIOLATION:
                raise _entry_unavailable() from None
            raise _capability_unavailable() from None
        except PlatformFileError:
            raise
        except Exception:
            raise _capability_unavailable() from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if records is not None:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _bind_root(self, root: Path) -> RootedDirectoryAuthority:
        _validate_probe_root(root)
        components = _validated_components(tuple(root.parts[1:]))
        records: list[_WindowsDirectoryRecord] = []
        try:
            api = self._native_api()
            drive_root = f"{root.drive}\\"
            drive_record = _open_directory_record(
                api,
                drive_root,
                None,
                stale=False,
            )
            guid_root = _guid_root_from_final_path(drive_record.expected_final_path)
            if guid_root != drive_record.expected_final_path:
                raise _outside_root()
            records.append(drive_record)
            process_machine, native_machine = _machine_facts(api)
            version = sys.getwindowsversion()
            _validate_runtime_facts(
                windows_major=int(version.major),
                windows_build=int(version.build),
                product_type=int(version.product_type),
                python_implementation=sys.implementation.name,
                python_version=(int(sys.version_info.major), int(sys.version_info.minor)),
                python_bits=struct.calcsize("P") * 8,
                process_machine=process_machine,
                native_machine=native_machine,
            )
            with drive_record.handle.borrow() as raw_drive:
                (
                    volume_serial,
                    file_system,
                    flags,
                    maximum_component_units,
                ) = _volume_facts_by_handle(api, raw_drive)
            del volume_serial
            if (
                _fixed_drive_type(api, guid_root) != DRIVE_FIXED
                or file_system != "NTFS"
                or not flags & FILE_PERSISTENT_ACLS
                or not flags & FILE_SUPPORTS_REPARSE_POINTS
            ):
                raise _capability_unavailable()
            current = guid_root
            components = _validated_components(
                components,
                maximum_units=maximum_component_units,
            )
            for component in components:
                current = _append_component(
                    current,
                    component,
                    maximum_units=maximum_component_units,
                )
                record = _open_directory_record(
                    api,
                    current,
                    current,
                    stale=False,
                    expected_volume_id=records[0].identity.volume_id,
                )
                records.append(record)
                _require_volume(
                    record.identity,
                    records[0].identity.volume_id,
                    stale=False,
                )
            transferred = tuple(records)
            records = []
            return _WindowsRootedDirectory(
                api,
                self._authority_issuer,
                object(),
                transferred,
                maximum_component_units,
                self._fault_injector,
            )
        except PlatformFileError:
            _close_handles_reverse(tuple(record.handle for record in records))
            raise
        except Exception:
            _close_handles_reverse(tuple(record.handle for record in records))
            raise _capability_unavailable() from None
        except BaseException:
            _close_handles_reverse(tuple(record.handle for record in records))
            raise

    def _open_regular(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundRegularFile:
        if not isinstance(root, _WindowsRootedDirectory):
            raise _capability_unavailable()
        components = _validated_components(
            tuple(relative.parts),
            maximum_units=root._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        owned_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        try:
            root._reprove()
            records = _duplicate_directory_chain(root._api, root._records)
            owned_records = records
            records = _open_directory_components(
                root._api,
                records,
                components[:-1],
                maximum_component_units=root._maximum_component_units,
            )
            owned_records = records
            entry_path = _append_component(
                records[-1].expected_final_path,
                components[-1],
                maximum_units=root._maximum_component_units,
            )
            probed = _open_entry_proof(
                root._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=records[0].identity.volume_id,
                stale=False,
                entry_unavailable=True,
                reject_wrong_kind=True,
            )
            if probed is None:
                raise _entry_unavailable()
            _hit_fault(self._fault_injector, "windows_after_entry_probe")
            try:
                handle = root._api.open_handle(
                    entry_path,
                    desired_access=GENERIC_READ | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                    share_mode=FILE_SHARE_READ,
                    creation_disposition=OPEN_EXISTING,
                    flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
                )
            except Win32CallError:
                raise _identity_stale() from None
            with handle.borrow() as raw:
                source = _capture_handle_proof(
                    root._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            if (
                not _same_object(probed.identity, source.identity)
                or probed.snapshot != source.snapshot
            ):
                raise _identity_stale()
            _require_volume(
                source.identity,
                records[0].identity.volume_id,
                stale=True,
            )
            transferred_records = records
            transferred_handle = handle
            owned_records = None
            handle = None
            return _WindowsBoundRegularFile(
                root._api,
                transferred_records,
                transferred_handle,
                entry_path,
                source.identity,
                root._maximum_component_units,
                self._fault_injector,
            )
        except PlatformFileError:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise
        except Exception:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise _capability_unavailable() from None
        except BaseException:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise

    def _open_existing_for_synchronization(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundSynchronizedRegularFile:
        if not isinstance(root, _WindowsRootedDirectory):
            raise _capability_unavailable()
        components = _validated_components(
            tuple(relative.parts),
            maximum_units=root._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        owned_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        try:
            root._reprove()
            records = _duplicate_directory_chain(root._api, root._records)
            owned_records = records
            records = _open_directory_components(
                root._api,
                records,
                components[:-1],
                maximum_component_units=root._maximum_component_units,
            )
            owned_records = records
            entry_path = _append_component(
                records[-1].expected_final_path,
                components[-1],
                maximum_units=root._maximum_component_units,
            )
            probed = _open_entry_proof(
                root._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=records[0].identity.volume_id,
                stale=False,
                entry_unavailable=True,
                reject_wrong_kind=True,
            )
            if probed is None:
                raise _entry_unavailable()
            if probed.identity.link_count != 1:
                raise _identity_stale()
            _hit_fault(
                self._fault_injector,
                "windows_synchronization_after_entry_probe",
            )
            try:
                handle = root._api.open_handle(
                    entry_path,
                    desired_access=(
                        GENERIC_READ
                        | GENERIC_WRITE
                        | FILE_READ_ATTRIBUTES
                        | SYNCHRONIZE
                    ),
                    share_mode=FILE_SHARE_READ,
                    creation_disposition=OPEN_EXISTING,
                    flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
                )
            except Win32CallError:
                raise _durability_unavailable() from None
            with handle.borrow() as raw:
                source = _capture_handle_proof(
                    root._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            if (
                probed.identity != source.identity
                or probed.snapshot != source.snapshot
                or source.identity.link_count != 1
            ):
                raise _identity_stale()
            _require_volume(
                source.identity,
                records[0].identity.volume_id,
                stale=True,
            )
            transferred_records = records
            transferred_handle = handle
            owned_records = None
            handle = None
            return _WindowsBoundSynchronizedRegularFile(
                root._api,
                transferred_records,
                transferred_handle,
                entry_path,
                source.identity,
                root._maximum_component_units,
                self._fault_injector,
            )
        except PlatformFileError:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise
        except Exception:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise _durability_unavailable() from None
        except BaseException:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise

    def _guard_existing_for_mutation(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundExistingFileMutationGuard:
        if not isinstance(root, _WindowsRootedDirectory):
            raise _capability_unavailable()
        components = _validated_components(
            tuple(relative.parts),
            maximum_units=root._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        owned_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        try:
            root._reprove()
            records = _duplicate_directory_chain(root._api, root._records)
            owned_records = records
            records = _open_directory_components(
                root._api,
                records,
                components[:-1],
                maximum_component_units=root._maximum_component_units,
            )
            owned_records = records
            entry_path = _append_component(
                records[-1].expected_final_path,
                components[-1],
                maximum_units=root._maximum_component_units,
            )
            probed = _open_entry_proof(
                root._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=records[0].identity.volume_id,
                stale=False,
                entry_unavailable=True,
                reject_wrong_kind=True,
            )
            if probed is None:
                raise _entry_unavailable()
            if probed.identity.link_count != 1:
                raise _identity_stale()
            _hit_fault(
                self._fault_injector,
                "windows_mutation_guard_after_entry_probe",
            )
            try:
                handle = root._api.open_handle(
                    entry_path,
                    desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                    share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
                    creation_disposition=OPEN_EXISTING,
                    flags=FILE_FLAG_OPEN_REPARSE_POINT,
                )
            except Win32CallError:
                raise _identity_stale() from None
            with handle.borrow() as raw:
                guarded = _capture_handle_proof(
                    root._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            if (
                probed.identity != guarded.identity
                or probed.snapshot != guarded.snapshot
                or guarded.identity.link_count != 1
            ):
                raise _identity_stale()
            transferred_records = records
            transferred_handle = handle
            owned_records = None
            handle = None
            return _WindowsExistingFileMutationGuard(
                root._api,
                root._issuer,
                root._root_anchor,
                transferred_records,
                transferred_handle,
                entry_path,
                guarded.identity,
            )
        except (TypeError, AssertionError, AttributeError):
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise
        except PlatformFileError:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise
        except Exception:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise _identity_stale() from None
        except BaseException:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if owned_records is not None:
                _close_handles_reverse(
                    tuple(record.handle for record in owned_records)
                )
            raise

    def _reserve_mutable_file(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> MutableFileReservation:
        if not isinstance(parent, _WindowsDirectoryAuthorityMixin):
            raise _capability_unavailable()
        component = _validate_windows_component(
            name,
            maximum_units=parent._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        created = False
        try:
            parent._reprove()
            records = _duplicate_directory_chain(parent._api, parent._records)
            entry_path = _append_component(
                records[-1].expected_final_path,
                component,
                maximum_units=parent._maximum_component_units,
            )
            handle = parent._api.open_handle(
                entry_path,
                desired_access=FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                creation_disposition=CREATE_NEW,
                flags=FILE_FLAG_OPEN_REPARSE_POINT,
            )
            created = True
            with handle.borrow() as raw:
                retained = _capture_handle_proof(
                    parent._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=True,
                )
            named = _open_entry_proof(
                parent._api,
                entry_path,
                entry_path,
                expected_kind="regular",
                expected_volume_id=records[0].identity.volume_id,
                stale=True,
            )
            if named is None or not (
                retained.snapshot.byte_count == 0
                and retained.identity.link_count == 1
                and named.identity.link_count == 1
                and _same_object(retained.identity, named.identity)
            ):
                raise _recovery_required()
            transferred_records = records
            transferred_handle = handle
            records = None
            handle = None
            return _WindowsMutableFileReservation(
                parent._api,
                transferred_records,
                transferred_handle,
                entry_path,
                retained.identity,
            )
        except (TypeError, AssertionError, AttributeError):
            raise
        except Win32CallError as error:
            if created:
                raise _recovery_required() from None
            if error.winerror in {ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS}:
                raise _entry_unavailable() from None
            raise _capability_unavailable() from None
        except PlatformFileError as error:
            if created and error.code != PlatformFileErrorCode.RECOVERY_REQUIRED.value:
                raise _recovery_required() from None
            raise
        except Exception:
            if created:
                raise _recovery_required() from None
            raise _capability_unavailable() from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if records is not None:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _bind_or_create_child_directory(
        self,
        parent: BoundDirectoryAuthority | RetirementDirectoryAuthority,
        name: str,
    ) -> RetirementDirectoryAuthority:
        if not isinstance(parent, _WindowsDirectoryAuthorityMixin):
            raise _capability_unavailable()
        component = _validate_windows_component(
            name,
            maximum_units=parent._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] | None = None
        leaf: _WindowsDirectoryRecord | None = None
        strict_parent: _WindowsDirectoryRecord | None = None
        compatible_parent: _WindowsDirectoryRecord | None = None
        created = False
        try:
            parent._reprove()
            if type(parent) is _WindowsRetirementTargetDirectory:
                strict_parent = _open_directory_record(
                    parent._api,
                    parent._leaf_path,
                    parent._leaf_path,
                    stale=True,
                    expected_volume_id=parent._records[0].identity.volume_id,
                )
                if strict_parent.identity != parent._records[-1].identity:
                    raise _identity_stale()
            records = _duplicate_directory_chain(parent._api, parent._records)
            if type(parent) in {_WindowsRootedDirectory, _WindowsBoundDirectory}:
                compatible_parent = _open_retirement_target_record(
                    parent._api,
                    parent._leaf_path,
                    records[0].identity.volume_id,
                    stale=True,
                )
                if compatible_parent.identity != records[-1].identity:
                    raise _identity_stale()
                replaced_parent = records[-1]
                records = records[:-1] + (compatible_parent,)
                compatible_parent = None
                replaced_parent.handle.close()
            path = _append_component(
                records[-1].expected_final_path,
                component,
                maximum_units=parent._maximum_component_units,
            )
            try:
                parent._api.checked_bool(
                    "CreateDirectoryW",
                    parent._api.CreateDirectoryW,
                    path,
                    None,
                )
                created = True
                _hit_fault(self._fault_injector, "retirement_directory_after_create")
            except Win32CallError as error:
                if error.winerror != ERROR_ALREADY_EXISTS:
                    raise
            leaf = _open_retirement_target_record(
                parent._api,
                path,
                records[0].identity.volume_id,
                stale=created,
            )
            parent._reprove()
            if (
                strict_parent is not None
                and strict_parent.identity != parent._records[-1].identity
            ):
                raise _identity_stale()
            transferred_records = records + (leaf,)
            records = None
            leaf = None
            return _WindowsRetirementTargetDirectory(
                parent._api,
                parent._issuer,
                parent._root_anchor,
                transferred_records,
                parent._maximum_component_units,
                self._fault_injector,
            )
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            if created:
                raise _recovery_required() from None
            raise
        except Exception:
            raise (
                _recovery_required() if created else _capability_unavailable()
            ) from None
        finally:
            if strict_parent is not None:
                try:
                    strict_parent.handle.close()
                except BaseException:
                    pass
            if compatible_parent is not None:
                try:
                    compatible_parent.handle.close()
                except BaseException:
                    pass
            if leaf is not None:
                try:
                    leaf.handle.close()
                except BaseException:
                    pass
            if records is not None:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _retire_owned_exclusive(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        reservation: MutableFileReservation,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement:
        if (
            not isinstance(source_parent, _WindowsDirectoryAuthorityMixin)
            or type(target_parent) is not _WindowsRetirementTargetDirectory
            or type(reservation) is not _WindowsMutableFileReservation
            or source_parent._api is not target_parent._api
            or reservation._api is not source_parent._api
        ):
            raise _capability_unavailable()
        if (
            reservation._entry_path
            != _append_component(
                source_parent._leaf_path,
                source_name,
                maximum_units=source_parent._maximum_component_units,
            )
            or len(reservation._records) != len(source_parent._records)
            or any(
                left.expected_final_path != right.expected_final_path
                or left.identity != right.identity
                for left, right in zip(
                    reservation._records,
                    source_parent._records,
                    strict=True,
                )
            )
        ):
            raise _identity_stale()
        reservation._require_open()
        _reprove_directory_chain(reservation._api, reservation._records)
        with reservation._handle.borrow() as raw_reservation:
            live_reservation = _capture_handle_proof(
                reservation._api,
                raw_reservation,
                expected_final_path=None,
                expected_kind="regular",
                stale=True,
            )
        owned = reservation._created_identity
        if live_reservation.identity != owned or owned.link_count != 1:
            raise _identity_stale()
        armed = False
        move_handle = None
        source_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        target_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        retained: _WindowsRetainedRetirement | None = None
        try:
            source_parent._reprove()
            target_parent._reprove()
            source_path = reservation._entry_path
            target_path = _append_component(
                target_parent._leaf_path,
                target_name,
                maximum_units=target_parent._maximum_component_units,
            )
            source = source_parent.inspect_entry(source_name)
            target = target_parent.inspect_entry(target_name)
            move_path: str
            if source is not None and source.identity == owned and target is None:
                move_path = source_path
            elif source is None and target is not None and target.identity == owned:
                move_path = target_path
                armed = True
            else:
                raise _recovery_required()
            move_handle = source_parent._api.open_handle(
                move_path,
                desired_access=DELETE | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=FILE_SHARE_READ | FILE_SHARE_WRITE,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT,
            )
            with move_handle.borrow() as raw_move:
                move_proof = _capture_handle_proof(
                    source_parent._api,
                    raw_move,
                    expected_final_path=move_path,
                    expected_kind="regular",
                    stale=True,
                )
                if move_proof.identity != owned or owned.link_count != 1:
                    raise _recovery_required()
                if not armed:
                    _hit_fault(self._fault_injector, "retirement_before_arm")
                    armed = True
                    with target_parent._records[-1].handle.borrow() as raw_target_parent:
                        source_parent._api.rename_file_to_parent_exclusive(
                            raw_move,
                            raw_target_parent,
                            target_name,
                        )
                    _hit_fault(self._fault_injector, "retirement_after_arm")
            move_handle.close()
            move_handle = source_parent._api.open_handle(
                target_path,
                desired_access=GENERIC_READ | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=FILE_SHARE_READ,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
            )
            with move_handle.borrow() as raw_retained:
                retained_proof = _capture_handle_proof(
                    source_parent._api,
                    raw_retained,
                    expected_final_path=target_path,
                    expected_kind="regular",
                    stale=True,
                )
            with reservation._handle.borrow() as raw_creator:
                live_creator = _capture_handle_proof(
                    source_parent._api,
                    raw_creator,
                    expected_final_path=None,
                    expected_kind="regular",
                    stale=True,
                )
            if (
                retained_proof.identity != owned
                or live_creator.identity != owned
                or retained_proof.identity != live_creator.identity
                or retained_proof.identity.link_count != 1
                or live_creator.identity.link_count != 1
            ):
                raise _recovery_required()
            source_records = _duplicate_directory_chain(
                source_parent._api,
                source_parent._records,
            )
            target_records = _duplicate_directory_chain(
                target_parent._api,
                target_parent._records,
            )
            retained = _WindowsRetainedRetirement(
                owned,
                source_parent._api,
                source_records,
                source_name,
                target_records,
                target_name,
                move_handle,
                target_path,
            )
            source_records = None
            target_records = None
            move_handle = None
            retained.reprove()
            retained._accept_reservation_transfer(reservation)
            result = retained
            retained = None
            return result
        except (TypeError, AssertionError, AttributeError):
            raise
        except Win32CallError as error:
            if armed:
                raise _recovery_required() from None
            if error.winerror == ERROR_SHARING_VIOLATION:
                raise _entry_unavailable() from None
            raise _capability_unavailable() from None
        except PlatformFileError:
            if armed:
                raise _recovery_required() from None
            raise
        except Exception:
            raise (
                _recovery_required() if armed else _capability_unavailable()
            ) from None
        finally:
            if retained is not None:
                try:
                    retained.close()
                except BaseException:
                    pass
            if move_handle is not None:
                try:
                    move_handle.close()
                except BaseException:
                    pass
            for records_to_close in (source_records, target_records):
                if records_to_close is not None:
                    _close_handles_reverse(
                        tuple(record.handle for record in records_to_close)
                    )

    def _retire_existing_exclusive(
        self,
        source_parent: RetirementSourceDirectoryAuthority,
        source: ExistingRetirementSource,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
    ) -> RetainedRetirement:
        if (
            type(source_parent) is not _WindowsRetirementSourceDirectoryAuthority
            or type(target_parent) is not _WindowsRetirementTargetDirectory
            or type(source) is not _WindowsExistingRetirementSource
            or source_parent._api is not target_parent._api
            or source._api is not source_parent._api
            or source_parent._issuer is not self._authority_issuer
            or target_parent._issuer is not self._authority_issuer
            or source._issuer is not self._authority_issuer
            or source_parent._root_anchor is not source._root_anchor
            or source_parent._root_anchor is not target_parent._root_anchor
            or not source._records
            or not target_parent._records
            or source._records[0].identity != source_parent._records[0].identity
            or target_parent._records[0].identity
            != source_parent._records[0].identity
        ):
            raise _capability_unavailable()
        source_token_path = _append_component(
            source._records[-1].expected_final_path,
            source._source_name,
            maximum_units=source._maximum_component_units,
        )
        if source_token_path != source._entry_path:
            raise _identity_stale()
        source_name = source_parent._source_name
        source_path = source_parent._entry_path
        if (
            source._entry_path != source_path
            or source._source_name != source_name
            or len(source._records) != len(source_parent._records)
            or any(
                left.expected_final_path != right.expected_final_path
                or left.identity != right.identity
                for left, right in zip(
                    source._records,
                    source_parent._records,
                    strict=True,
                )
            )
        ):
            raise _identity_stale()
        target_path = _append_component(
            target_parent._leaf_path,
            target_name,
            maximum_units=target_parent._maximum_component_units,
        )
        armed = False
        source_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        target_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        transferred_handle = None
        retained: _WindowsRetainedRetirement | None = None
        try:
            expected_content = source._expected_content
            source_facts = source.reprove()
            source_parent._reprove_parent()
            target_parent._reprove()
            named_source = source_parent._inspect_source(stale=True)
            named_target = target_parent.inspect_entry(target_name)
            if (
                named_source is None
                or named_source != source_facts.snapshot
                or named_target is not None
            ):
                raise _recovery_required()
            _hit_fault(self._fault_injector, "existing_retirement_before_arm")
            armed = True
            with source._handle.borrow() as raw_source:
                with target_parent._records[-1].handle.borrow() as raw_target_parent:
                    source_parent._api.rename_file_to_parent_exclusive(
                        raw_source,
                        raw_target_parent,
                        target_name,
                    )
            _hit_fault(self._fault_injector, "existing_retirement_after_arm")
            terminal_source = source_parent._inspect_source(stale=True)
            terminal_target = target_parent.inspect_entry(target_name)
            with source._handle.borrow() as raw_source:
                moved = _capture_handle_proof(
                    source_parent._api,
                    raw_source,
                    expected_final_path=target_path,
                    expected_kind="regular",
                    stale=True,
                )
            if (
                terminal_source is not None
                or terminal_target is None
                or terminal_target.identity != source_facts.snapshot.identity
                or moved.identity != source_facts.snapshot.identity
                or terminal_target.identity.link_count != 1
                or moved.identity.link_count != 1
            ):
                raise _recovery_required()
            target_records = _duplicate_directory_chain(
                target_parent._api,
                target_parent._records,
            )
            (
                transferred_api,
                source_records,
                transferred_handle,
                _old_source_path,
                transferred_identity,
                transferred_content,
            ) = source._transfer_live_authority(target_path)
            if (
                transferred_api is not source_parent._api
                or transferred_identity != source_facts.snapshot.identity
                or transferred_content != expected_content
            ):
                raise _recovery_required()
            adopted_source_records = source_records
            adopted_target_records = target_records
            adopted_handle = transferred_handle
            retained = _WindowsRetainedRetirement(
                transferred_identity,
                transferred_api,
                adopted_source_records,
                source_name,
                adopted_target_records,
                target_name,
                adopted_handle,
                target_path,
                expected_content=expected_content,
                fault_injector=self._fault_injector,
            )
            source_records = None
            target_records = None
            transferred_handle = None
            retained.reprove()
            result = retained
            retained = None
            return result
        except (TypeError, AssertionError, AttributeError):
            raise
        except Win32CallError:
            if armed:
                raise _recovery_required() from None
            raise _entry_unavailable() from None
        except PlatformFileError:
            if armed:
                raise _recovery_required() from None
            raise
        except Exception:
            raise (
                _recovery_required() if armed else _capability_unavailable()
            ) from None
        finally:
            if retained is not None:
                try:
                    retained.close()
                except BaseException:
                    pass
            if transferred_handle is not None:
                try:
                    transferred_handle.close()
                except BaseException:
                    pass
            for records_to_close in (source_records, target_records):
                if records_to_close is not None:
                    _close_handles_reverse(
                        tuple(record.handle for record in records_to_close)
                    )

    def _rebind_existing_retirement(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
    ) -> RetainedRetirement:
        if (
            type(source_parent) is not _WindowsBoundDirectory
            or type(target_parent) is not _WindowsRetirementTargetDirectory
            or source_parent._api is not target_parent._api
            or source_parent._issuer is not self._authority_issuer
            or target_parent._issuer is not self._authority_issuer
            or source_parent._root_anchor is not target_parent._root_anchor
            or not source_parent._records
            or not target_parent._records
            or source_parent._records[0].identity
            != target_parent._records[0].identity
        ):
            raise _capability_unavailable()
        target_path = _append_component(
            target_parent._leaf_path,
            target_name,
            maximum_units=target_parent._maximum_component_units,
        )
        source_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        target_records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        retained: _WindowsRetainedRetirement | None = None
        try:
            source_parent._reprove()
            target_parent._reprove()
            if source_parent.inspect_entry(source_name) is not None:
                raise _recovery_required()
            probed = target_parent.inspect_entry(target_name)
            if (
                probed is None
                or probed.identity.kind != "regular"
                or probed.identity.link_count != 1
                or probed.byte_count != expected_content.byte_count
            ):
                raise _recovery_required()
            handle = source_parent._api.open_handle(
                target_path,
                desired_access=GENERIC_READ | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=FILE_SHARE_READ,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
            )
            with handle.borrow() as raw:
                opened = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=target_path,
                    expected_kind="regular",
                    stale=True,
                )
            actual = _read_publish_facts(
                source_parent._api,
                handle,
                opened.snapshot.byte_count,
            )
            with handle.borrow() as raw:
                terminal = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=target_path,
                    expected_kind="regular",
                    stale=True,
                )
            terminal_named = target_parent.inspect_entry(target_name)
            terminal_source = source_parent.inspect_entry(source_name)
            if (
                actual != expected_content
                or probed != opened.snapshot
                or opened.snapshot != terminal.snapshot
                or terminal_named != terminal.snapshot
                or terminal_source is not None
                or terminal.identity.link_count != 1
            ):
                raise _recovery_required()
            source_parent._reprove()
            target_parent._reprove()
            _hit_fault(
                self._fault_injector,
                "existing_retirement_rebind_before_terminal",
            )
            source_records = _duplicate_directory_chain(
                source_parent._api,
                source_parent._records,
            )
            target_records = _duplicate_directory_chain(
                target_parent._api,
                target_parent._records,
            )
            adopted_source_records = source_records
            adopted_target_records = target_records
            adopted_handle = handle
            retained = _WindowsRetainedRetirement(
                terminal.identity,
                source_parent._api,
                adopted_source_records,
                source_name,
                adopted_target_records,
                target_name,
                adopted_handle,
                target_path,
                expected_content=expected_content,
                fault_injector=self._fault_injector,
            )
            source_records = None
            target_records = None
            handle = None
            retained.reprove()
            result = retained
            retained = None
            return result
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError:
            raise _recovery_required() from None
        except Exception:
            raise _recovery_required() from None
        finally:
            if retained is not None:
                try:
                    retained.close()
                except BaseException:
                    pass
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            for records_to_close in (source_records, target_records):
                if records_to_close is not None:
                    _close_handles_reverse(
                        tuple(record.handle for record in records_to_close)
                    )

    def _recover_existing_candidate(
        self,
        source_parent: BoundDirectoryAuthority,
        source_name: str,
        target_parent: RetirementDirectoryAuthority,
        target_name: str,
        expected_content: CandidateContentFacts,
        *,
        owner_lease: LockLease,
        mutation_guard: BoundExistingFileMutationGuard,
        require_private: bool,
    ) -> CandidateFile:
        if (
            type(source_parent)
            not in {_WindowsRootedDirectory, _WindowsBoundDirectory}
            or type(target_parent) is not _WindowsRetirementTargetDirectory
            or source_parent._api is not target_parent._api
            or source_parent._issuer is not self._authority_issuer
            or target_parent._issuer is not self._authority_issuer
            or source_parent._root_anchor is not target_parent._root_anchor
            or not source_parent._records
            or not target_parent._records
            or source_parent._records[0].identity
            != target_parent._records[0].identity
            or type(owner_lease) is not _WindowsLockLease
            or type(mutation_guard) is not _WindowsExistingFileMutationGuard
            or owner_lease._api is not source_parent._api
            or mutation_guard._api is not source_parent._api
            or owner_lease._issuer is not source_parent._issuer
            or mutation_guard._issuer is not source_parent._issuer
            or owner_lease._root_anchor is not source_parent._root_anchor
            or mutation_guard._root_anchor is not source_parent._root_anchor
            or not owner_lease._matches_parent(
                source_parent._api,
                source_parent._issuer,
                source_parent._records,
            )
            or not mutation_guard._matches_parent(
                source_parent._api,
                source_parent._issuer,
                source_parent._records,
            )
        ):
            raise _capability_unavailable()
        source_path = _append_component(
            source_parent._leaf_path,
            source_name,
            maximum_units=source_parent._maximum_component_units,
        )
        target_path = _append_component(
            target_parent._leaf_path,
            target_name,
            maximum_units=target_parent._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] | None = None
        handle = None
        candidate: _WindowsCandidateFile | None = None
        source_window: _WindowsDirectoryRenameWindow | None = None
        lease_window: _WindowsDirectoryRenameWindow | None = None
        guard_window: _WindowsDirectoryRenameWindow | None = None

        def restore_owner_windows() -> None:
            nonlocal source_window, lease_window, guard_window
            first_error: BaseException | None = None
            for window, label in (
                (source_window, "source"),
                (guard_window, "guard"),
                (lease_window, "lease"),
            ):
                if window is None:
                    continue
                try:
                    window.restore()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
                else:
                    if label == "source":
                        source_window = None
                    elif label == "guard":
                        guard_window = None
                    else:
                        lease_window = None
            if first_error is not None:
                raise first_error
            source_parent._reprove()
            target_parent._reprove()
            owner_lease._reprove_lock()
            mutation_guard._reprove_guard()

        try:
            owner_lease._reprove_lock()
            mutation_guard._reprove_guard()
            source_parent._reprove()
            target_parent._reprove()
            source_snapshot = source_parent.inspect_entry(source_name)
            target_snapshot = target_parent.inspect_entry(target_name)
            if (source_snapshot is None) == (target_snapshot is None):
                raise _recovery_required()
            selected = (
                source_snapshot if source_snapshot is not None else target_snapshot
            )
            if (
                selected is None
                or selected.identity.kind != "regular"
                or selected.identity.link_count != 1
                or not selected.reparse_free
                or selected.byte_count != expected_content.byte_count
            ):
                raise _recovery_required()
            selected_path = source_path if source_snapshot is not None else target_path
            handle = source_parent._api.open_handle(
                selected_path,
                desired_access=(
                    (
                        GENERIC_READ
                        | DELETE
                        | FILE_READ_ATTRIBUTES
                        | SYNCHRONIZE
                        | (READ_CONTROL if require_private else 0)
                    )
                    if source_snapshot is None
                    else GENERIC_READ
                    | GENERIC_WRITE
                    | DELETE
                    | FILE_READ_ATTRIBUTES
                    | READ_CONTROL
                    | SYNCHRONIZE
                ),
                share_mode=FILE_SHARE_READ if source_snapshot is None else 0,
                creation_disposition=OPEN_EXISTING,
                flags=(
                    FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN
                    if source_snapshot is None
                    else FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH
                ),
            )
            with handle.borrow() as raw:
                opened = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=selected_path,
                    expected_kind="regular",
                    stale=True,
                )
            actual = _read_publish_facts(
                source_parent._api,
                handle,
                opened.snapshot.byte_count,
            )
            with handle.borrow() as raw:
                after_read = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=selected_path,
                    expected_kind="regular",
                    stale=True,
                )
                if require_private:
                    _private_security_facts(source_parent._api, raw)
            if (
                selected != opened.snapshot
                or opened.snapshot != after_read.snapshot
                or opened.identity.link_count != 1
                or actual != expected_content
            ):
                raise _recovery_required()
            source_parent._reprove()
            target_parent._reprove()
            if source_snapshot is None:
                if (
                    source_parent.inspect_entry(source_name) is not None
                    or target_parent.inspect_entry(target_name) != after_read.snapshot
                ):
                    raise _recovery_required()
                lease_window = owner_lease._begin_directory_rename_window(
                    source_parent
                )
                guard_window = mutation_guard._begin_directory_rename_window(
                    source_parent
                )
                old_leaf = source_parent._records[-1]
                source_window = _begin_directory_rename_window(
                    source_parent._api,
                    source_parent,
                    old_leaf,
                )
                source_parent._reprove()
                target_parent._reprove()
                _hit_fault(
                    self._fault_injector,
                    "existing_candidate_recovery_before_restore",
                )
                with handle.borrow() as raw_candidate:
                    with source_parent._records[-1].handle.borrow() as raw_source_parent:
                        source_parent._api.rename_file_to_parent_exclusive(
                            raw_candidate,
                            raw_source_parent,
                            source_name,
                        )
                selected_path = source_path
                _hit_fault(
                    self._fault_injector,
                    "existing_candidate_recovery_after_restore",
                )
                handle.close()
                handle = source_parent._api.open_handle(
                    source_path,
                    desired_access=(
                        GENERIC_READ
                        | GENERIC_WRITE
                        | DELETE
                        | FILE_READ_ATTRIBUTES
                        | READ_CONTROL
                        | SYNCHRONIZE
                    ),
                    share_mode=0,
                    creation_disposition=OPEN_EXISTING,
                    flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_WRITE_THROUGH,
                )
                restore_owner_windows()
            elif (
                source_parent.inspect_entry(source_name) != after_read.snapshot
                or target_parent.inspect_entry(target_name) is not None
            ):
                raise _recovery_required()
            try:
                with handle.borrow() as raw:
                    source_parent._api.checked_bool(
                        "FlushFileBuffers",
                        source_parent._api.FlushFileBuffers,
                        raw,
                    )
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                raise _durability_unavailable() from None
            synchronized = _read_publish_facts(
                source_parent._api,
                handle,
                expected_content.byte_count,
            )
            with handle.borrow() as raw:
                terminal = _capture_handle_proof(
                    source_parent._api,
                    raw,
                    expected_final_path=source_path,
                    expected_kind="regular",
                    stale=True,
                )
            terminal_source = source_parent.inspect_entry(source_name)
            terminal_target = target_parent.inspect_entry(target_name)
            if (
                synchronized != expected_content
                or terminal.identity != selected.identity
                or terminal.identity.link_count != 1
                or terminal_source != terminal.snapshot
                or terminal_target is not None
            ):
                raise _recovery_required()
            source_parent._reprove()
            target_parent._reprove()
            # No candidate authority may escape until both owner authorities
            # are back on their strict directory records and every retained
            # authority participating in the move has been reproved.
            restore_owner_windows()
            source_parent._reprove()
            target_parent._reprove()
            _hit_fault(
                self._fault_injector,
                "existing_candidate_recovery_before_terminal",
            )
            records = _duplicate_directory_chain(
                source_parent._api,
                source_parent._records,
            )
            candidate = _WindowsCandidateFile(
                source_parent._api,
                records,
                handle,
                source_path,
                terminal.identity,
                source_parent._maximum_component_units,
                self._fault_injector,
                private=require_private,
                recovered_content=expected_content,
            )
            records = None
            handle = None
            result = candidate
            candidate = None
            return result
        except (TypeError, AssertionError, AttributeError):
            raise
        except PlatformFileError as error:
            if error.code == PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value:
                raise
            raise _recovery_required() from None
        except Exception:
            raise _recovery_required() from None
        finally:
            restore_error: BaseException | None = None
            if (
                source_window is not None
                or lease_window is not None
                or guard_window is not None
            ):
                try:
                    restore_owner_windows()
                except BaseException as error:
                    restore_error = error
            if candidate is not None:
                try:
                    candidate.close()
                except BaseException:
                    pass
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if records is not None:
                _close_handles_reverse(tuple(record.handle for record in records))
            if restore_error is not None and sys.exception() is None:
                raise _recovery_required() from None

    def _bind_parent(
        self,
        root: RootedDirectoryAuthority,
        relative: PurePath,
    ) -> BoundDirectoryAuthority:
        if not isinstance(root, _WindowsRootedDirectory):
            raise _capability_unavailable()
        components = _validated_components(
            tuple(relative.parts),
            maximum_units=root._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        try:
            root._reprove()
            records = _duplicate_directory_chain(root._api, root._records)
            expanded = _open_directory_components(
                root._api,
                records,
                components[:-1],
                maximum_component_units=root._maximum_component_units,
            )
            if expanded is not records:
                records = ()
            return _WindowsBoundDirectory(
                root._api,
                root._issuer,
                root._root_anchor,
                expanded,
                root._maximum_component_units,
                self._fault_injector,
            )
        except PlatformFileError:
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))
            raise
        except Exception:
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))
            raise _capability_unavailable() from None
        except BaseException:
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))
            raise


# Task 3.4 private storage and persistent re-attestation.  These authorities are
# deliberately concrete, issuer-bound, and separate from the W1 lock objects.
TOKEN_TYPE_CLASS = 8
TOKEN_SESSION_ID_CLASS = 12
TOKEN_INTEGRITY_LEVEL_CLASS = 25
TOKEN_IS_APP_CONTAINER_CLASS = 29
TOKEN_PRIMARY = 1
MEDIUM_INTEGRITY_RID = 0x2000
HIGH_INTEGRITY_RID = 0x3000


@dataclass(frozen=True, slots=True)
class _WindowsPrivateSecurityFacts:
    owner_sid: bytes
    descriptor_sha256: bytes


def _private_unproven() -> PlatformFileError:
    return _platform_failure(
        PlatformFileErrorCode.PRIVATE_STORAGE_UNPROVEN,
        retryable=False,
    )


def _recovery_required() -> PlatformFileError:
    return _platform_failure(
        PlatformFileErrorCode.RECOVERY_REQUIRED,
        retryable=True,
    )


def _private_token_dword(
    api: WindowsFileAPI,
    raw_token: int,
    information_class: int,
) -> int:
    value = DWORD()
    returned = DWORD()
    api.checked_bool(
        "GetTokenInformation",
        api.GetTokenInformation,
        raw_token,
        information_class,
        ctypes.byref(value),
        ctypes.sizeof(value),
        ctypes.byref(returned),
    )
    if int(returned.value) != ctypes.sizeof(value):
        raise _private_unproven()
    return int(value.value)


def _private_token_buffer(
    api: WindowsFileAPI,
    raw_token: int,
    information_class: int,
) -> ctypes.Array[ctypes.c_char]:
    needed = DWORD()
    result = api.GetTokenInformation(
        raw_token,
        information_class,
        None,
        0,
        ctypes.byref(needed),
    )
    error = api.last_error()
    if result or error != ERROR_INSUFFICIENT_BUFFER:
        raise _private_unproven()
    length = int(needed.value)
    if length < ctypes.sizeof(TOKEN_USER) or length > 65536:
        raise _private_unproven()
    buffer = ctypes.create_string_buffer(length)
    api.checked_bool(
        "GetTokenInformation",
        api.GetTokenInformation,
        raw_token,
        information_class,
        buffer,
        length,
        ctypes.byref(needed),
    )
    if int(needed.value) > length:
        raise _private_unproven()
    return buffer


def _integrity_rid_from_sid(value: bytes) -> int:
    if (
        len(value) != 12
        or value[0] != 1
        or value[1] != 1
        or int.from_bytes(value[2:8], "big") != 16
    ):
        raise _private_unproven()
    return int.from_bytes(value[8:12], "little")


def _private_primary_user_sid(api: WindowsFileAPI) -> bytes:
    token = None
    try:
        token = _open_current_primary_token(api, TOKEN_QUERY | TOKEN_DUPLICATE)
        with token.borrow() as raw_token:
            user_sid = _token_user_sid(api, raw_token)
            token_type = _private_token_dword(api, raw_token, TOKEN_TYPE_CLASS)
            session_id = _private_token_dword(api, raw_token, TOKEN_SESSION_ID_CLASS)
            elevation_type = _private_token_dword(
                api,
                raw_token,
                TOKEN_ELEVATION_TYPE_CLASS,
            )
            is_app_container = _private_token_dword(
                api,
                raw_token,
                TOKEN_IS_APP_CONTAINER_CLASS,
            )
            integrity_buffer = _private_token_buffer(
                api,
                raw_token,
                TOKEN_INTEGRITY_LEVEL_CLASS,
            )
            label = ctypes.cast(
                integrity_buffer,
                ctypes.POINTER(ctypes.c_void_p),
            ).contents
            integrity_sid = _sid_bytes(api, label.value)
            integrity_rid = _integrity_rid_from_sid(integrity_sid)
            restricted = bool(api.IsTokenRestricted(raw_token))
        if (
            token_type != TOKEN_PRIMARY
            or session_id < 1
            or elevation_type
            not in {
                TOKEN_ELEVATION_TYPE_DEFAULT,
                TOKEN_ELEVATION_TYPE_FULL,
                TOKEN_ELEVATION_TYPE_LIMITED,
            }
            or is_app_container != 0
            or restricted
            or integrity_rid not in {MEDIUM_INTEGRITY_RID, HIGH_INTEGRITY_RID}
        ):
            raise _private_unproven()
        return user_sid
    except PlatformFileError:
        raise _private_unproven() from None
    except Exception:
        raise _private_unproven() from None
    finally:
        if token is not None:
            active = sys.exception()
            try:
                token.close()
            except BaseException:
                if active is None:
                    raise _private_unproven() from None


def _private_descriptor_projection(owner_sid: bytes) -> bytes:
    def frame(value: bytes) -> bytes:
        return len(value).to_bytes(2, "little") + value

    dacl_entries = (
        (ACCESS_ALLOWED_ACE_TYPE, 0, FILE_ALL_ACCESS, owner_sid),
        (ACCESS_ALLOWED_ACE_TYPE, 0, FILE_ALL_ACCESS, _LOCAL_SYSTEM_SID),
        (
            ACCESS_ALLOWED_ACE_TYPE,
            0,
            FILE_ALL_ACCESS,
            _BUILTIN_ADMINISTRATORS_SID,
        ),
    )
    label_entry = (
        SYSTEM_MANDATORY_LABEL_ACE_TYPE,
        0,
        SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
        _MEDIUM_INTEGRITY_SID,
    )
    projection = bytearray(b"localcat.windows.private-security-v2\0")
    projection.extend(frame(owner_sid))
    projection.extend(
        struct.pack(
            "<III",
            SE_DACL_PRESENT | SE_DACL_PROTECTED,
            ACL_REVISION,
            len(dacl_entries),
        )
    )
    for ace_type, ace_flags, mask, sid in dacl_entries:
        projection.extend(struct.pack("<BBI", ace_type, ace_flags, mask))
        projection.extend(frame(sid))
    projection.extend(struct.pack("<II", ACL_REVISION, 1))
    ace_type, ace_flags, mask, sid = label_entry
    projection.extend(struct.pack("<BBI", ace_type, ace_flags, mask))
    projection.extend(frame(sid))
    return bytes(projection)


def _private_security_facts(
    api: WindowsFileAPI,
    raw_handle: int,
) -> _WindowsPrivateSecurityFacts:
    try:
        owner_sid = _private_primary_user_sid(api)
        _verify_exact_security_projection(api, raw_handle, owner_sid)
        return _WindowsPrivateSecurityFacts(
            owner_sid=owner_sid,
            descriptor_sha256=hashlib.sha256(
                _private_descriptor_projection(owner_sid)
            ).digest(),
        )
    except PlatformFileError:
        raise _private_unproven() from None
    except Exception:
        raise _private_unproven() from None


def _open_private_security_handle(
    api: WindowsFileAPI,
    path: str,
    *,
    kind: str,
):
    return api.open_handle(
        path,
        desired_access=(
            FILE_READ_ATTRIBUTES
            | READ_CONTROL
            | SYNCHRONIZE
            | (FILE_LIST_DIRECTORY if kind == "directory" else GENERIC_READ)
        ),
        share_mode=FILE_SHARE_READ,
        creation_disposition=OPEN_EXISTING,
        flags=(
            FILE_FLAG_OPEN_REPARSE_POINT
            | (FILE_FLAG_BACKUP_SEMANTICS if kind == "directory" else 0)
        ),
    )


@contextmanager
def _private_creation_security(
    api: WindowsFileAPI,
    user_sid: bytes,
):
    descriptor = PSECURITY_DESCRIPTOR()
    descriptor_size = DWORD()
    try:
        sddl = (
            f"O:{_sid_string(user_sid)}"
            f"D:P(A;;FA;;;{_sid_string(user_sid)})(A;;FA;;;SY)(A;;FA;;;BA)"
            "S:(ML;;NW;;;ME)"
        )
        api.checked_bool(
            "ConvertStringSecurityDescriptorToSecurityDescriptorW",
            api.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            sddl,
            SDDL_REVISION_1,
            ctypes.byref(descriptor),
            ctypes.byref(descriptor_size),
        )
        if not descriptor or int(descriptor_size.value) < 20:
            raise _private_unproven()
        owned = _LocalSecurityDescriptor(api, descriptor)
        descriptor = PSECURITY_DESCRIPTOR()
        try:
            yield owned.attributes
        finally:
            active = sys.exception()
            try:
                owned.close()
            except BaseException:
                if active is None:
                    raise _private_unproven() from None
    except PlatformFileError:
        raise
    except Exception:
        raise _private_unproven() from None
    finally:
        if descriptor:
            active = sys.exception()
            try:
                api.checked_local_free(descriptor)
            except BaseException:
                if active is None:
                    raise _private_unproven() from None


class _WindowsPrivateAccessEvidence(PrivateAccessEvidence):
    __slots__ = (
        "__issuer",
        "__api",
        "__records",
        "__handle",
        "__entry_path",
        "__identity",
        "__kind",
        "__owner_sid",
        "__descriptor_sha256",
    )

    def __init__(
        self,
        issuer: object,
        api: WindowsFileAPI,
        records: tuple[_WindowsDirectoryRecord, ...],
        handle: object | None,
        entry_path: str,
        identity: FileObjectIdentity,
        kind: str,
        facts: _WindowsPrivateSecurityFacts,
    ) -> None:
        super().__init__()
        self.__issuer = issuer
        self.__api = api
        self.__records = records
        self.__handle = handle
        self.__entry_path = entry_path
        self.__identity = identity
        self.__kind = kind
        self.__owner_sid = facts.owner_sid
        self.__descriptor_sha256 = facts.descriptor_sha256
        try:
            self._reprove_details()
        except BaseException:
            self._close_parts()
            raise

    def _issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def _owner_sid_digest(self) -> bytes:
        return hashlib.sha256(self.__owner_sid).digest()

    def _descriptor_digest(self) -> bytes:
        return self.__descriptor_sha256

    def _reprove_details(self) -> _WindowsHandleProof:
        try:
            _reprove_directory_chain(self.__api, self.__records)
            if self.__kind == "directory":
                if self.__handle is None:
                    raise _private_unproven()
                with self.__handle.borrow() as raw:
                    proof = _capture_handle_proof(
                        self.__api,
                        raw,
                        expected_final_path=self.__entry_path,
                        expected_kind="directory",
                        stale=True,
                    )
                    facts = _private_security_facts(self.__api, raw)
            else:
                if self.__handle is None:
                    raise _private_unproven()
                with self.__handle.borrow() as raw:
                    proof = _capture_handle_proof(
                        self.__api,
                        raw,
                        expected_final_path=self.__entry_path,
                        expected_kind="regular",
                        stale=True,
                    )
                    facts = _private_security_facts(self.__api, raw)
                named = _open_entry_proof(
                    self.__api,
                    self.__entry_path,
                    self.__entry_path,
                    expected_kind="regular",
                    expected_volume_id=self.__records[0].identity.volume_id,
                    stale=True,
                )
                if named is None or named.identity != proof.identity:
                    raise _private_unproven()
            if (
                proof.identity != self.__identity
                or proof.identity.volume_id != self.__records[0].identity.volume_id
                or (self.__kind == "regular" and proof.identity.link_count != 1)
                or facts.owner_sid != self.__owner_sid
                or facts.descriptor_sha256 != self.__descriptor_sha256
            ):
                raise _private_unproven()
            return proof
        except PlatformFileError:
            raise _private_unproven() from None
        except Exception:
            raise _private_unproven() from None

    def _read_device_secret(self) -> bytes:
        if self.__kind != "regular" or self.__handle is None:
            raise _private_unproven()
        before = self._reprove_details()
        if before.snapshot.byte_count != DEVICE_SECRET_SIZE_BYTES:
            raise _private_unproven()
        try:
            with self.__handle.borrow() as raw:
                payload = _read_exact_count(
                    self.__api,
                    raw,
                    DEVICE_SECRET_SIZE_BYTES,
                )
        except PlatformFileError:
            raise _private_unproven() from None
        except Exception:
            raise _private_unproven() from None
        after = self._reprove_details()
        if before.snapshot != after.snapshot or len(payload) != DEVICE_SECRET_SIZE_BYTES:
            raise _private_unproven()
        return payload

    def _clone(self) -> _WindowsPrivateAccessEvidence:
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        handle = None
        try:
            self._reprove_details()
            records = _duplicate_directory_chain(self.__api, self.__records)
            if self.__handle is not None:
                handle = self.__api.duplicate_handle(self.__handle)
            transferred_records = records
            transferred_handle = handle
            records = ()
            handle = None
            return _WindowsPrivateAccessEvidence(
                self.__issuer,
                self.__api,
                transferred_records,
                transferred_handle,
                self.__entry_path,
                self.__identity,
                self.__kind,
                _WindowsPrivateSecurityFacts(
                    self.__owner_sid,
                    self.__descriptor_sha256,
                ),
            )
        except PlatformFileError:
            raise
        except Exception:
            raise _private_unproven() from None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except BaseException:
                    pass
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _close_parts(self) -> BaseException | None:
        first: BaseException | None = None
        if self.__handle is not None:
            try:
                self.__handle.close()
            except BaseException as error:
                first = error
        chain_error = _close_handles_reverse(
            tuple(record.handle for record in self.__records)
        )
        return first if first is not None else chain_error

    def _close_authority(self) -> None:
        error = self._close_parts()
        if error is not None:
            if not isinstance(error, Exception):
                raise error
            raise _private_unproven() from None


def _private_evidence_from_authority(
    issuer: object,
    authority: BoundDirectoryAuthority | BoundRegularFile,
) -> _WindowsPrivateAccessEvidence:
    records: tuple[_WindowsDirectoryRecord, ...] = ()
    handle = None
    try:
        if type(authority) in {_WindowsBoundDirectory, _WindowsRootedDirectory}:
            authority._reprove()
            api = authority._api
            records = _duplicate_directory_chain(api, authority._records)
            entry_path = records[-1].expected_final_path
            identity = records[-1].identity
            handle = _open_private_security_handle(
                api,
                entry_path,
                kind="directory",
            )
            with handle.borrow() as raw:
                proof = _capture_handle_proof(
                    api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="directory",
                    stale=True,
                )
                facts = _private_security_facts(api, raw)
            if proof.identity != identity:
                raise _private_unproven()
            kind = "directory"
        elif type(authority) is _WindowsBoundRegularFile:
            proof = authority._reprove()
            api = authority._api
            records = _duplicate_directory_chain(api, authority._records)
            handle = api.duplicate_handle(authority._handle)
            with handle.borrow() as raw:
                facts = _private_security_facts(api, raw)
            entry_path = authority._entry_path
            identity = proof.identity
            kind = "regular"
        else:
            raise _private_unproven()
        transferred_records = records
        transferred_handle = handle
        records = ()
        handle = None
        return _WindowsPrivateAccessEvidence(
            issuer,
            api,
            transferred_records,
            transferred_handle,
            entry_path,
            identity,
            kind,
            facts,
        )
    except PlatformFileError:
        raise _private_unproven() from None
    except Exception:
        raise _private_unproven() from None
    finally:
        if handle is not None:
            try:
                handle.close()
            except BaseException:
                pass
        if records:
            _close_handles_reverse(tuple(record.handle for record in records))


class _WindowsDeviceSecretAuthority(DeviceSecretAuthority):
    __slots__ = ("__issuer", "__target")

    def __init__(
        self,
        issuer: object,
        target: _WindowsPrivateAccessEvidence,
    ) -> None:
        super().__init__()
        self.__issuer = issuer
        self.__target = target
        try:
            self.__target._read_device_secret()
        except BaseException:
            self.__target.close()
            raise

    def _issued_by(self, issuer: object) -> bool:
        return self.__issuer is issuer

    def _secret_bytes(self) -> bytes:
        return self.__target._read_device_secret()

    def _clone(self) -> _WindowsDeviceSecretAuthority:
        return _WindowsDeviceSecretAuthority(self.__issuer, self.__target._clone())

    def _reprove(self) -> None:
        proof = self.__target._reprove_details()
        if proof.snapshot.byte_count != DEVICE_SECRET_SIZE_BYTES:
            raise _private_unproven()

    def _close_authority(self) -> None:
        self.__target.close()


class _WindowsVerifiedPrivateProof(VerifiedPrivateProof):
    __slots__ = (
        "__issuer",
        "__target",
        "__secret",
        "__proof",
        "__context",
    )

    def __init__(
        self,
        issuer: object,
        target: _WindowsPrivateAccessEvidence,
        secret: _WindowsDeviceSecretAuthority,
        proof: WindowsPrivateProof,
        context: PrivateProofContext,
    ) -> None:
        super().__init__()
        self.__issuer = issuer
        self.__target = target
        self.__secret = secret
        self.__proof = proof
        self.__context = context

    def _matches(self, issuer: object, context: PrivateProofContext) -> bool:
        return self.__issuer is issuer and self.__context == context

    def _terminal_reproof(self) -> None:
        _verify_private_proof_material(
            self.__target,
            self.__secret,
            self.__proof,
            self.__context,
        )

    def _close_authority(self) -> None:
        first: BaseException | None = None
        try:
            self.__target.close()
        except BaseException as error:
            first = error
        try:
            self.__secret.close()
        except BaseException as error:
            if first is None:
                first = error
        if first is not None:
            if not isinstance(first, Exception):
                raise first
            raise _private_unproven() from None


def _expected_windows_private_proof(
    target: _WindowsPrivateAccessEvidence,
    secret_bytes: bytes,
    context: PrivateProofContext,
) -> WindowsPrivateProof:
    unsigned = WindowsPrivateProof(
        schema=WINDOWS_PRIVATE_PROOF_SCHEMA,
        object_role=context.object_role,
        security_profile_id=WINDOWS_PRIVATE_SECURITY_PROFILE_ID,
        owner_sid_sha256=target._owner_sid_digest(),
        authority_descriptor_sha256=target._descriptor_digest(),
        owner_context_sha256=context.owner_context_sha256,
        device_key_id=derive_device_key_id(secret_bytes),
        device_secret_mac=b"\0" * 32,
    )
    return WindowsPrivateProof(
        schema=unsigned.schema,
        object_role=unsigned.object_role,
        security_profile_id=unsigned.security_profile_id,
        owner_sid_sha256=unsigned.owner_sid_sha256,
        authority_descriptor_sha256=unsigned.authority_descriptor_sha256,
        owner_context_sha256=unsigned.owner_context_sha256,
        device_key_id=unsigned.device_key_id,
        device_secret_mac=hmac.digest(
            secret_bytes,
            windows_private_proof_mac_message(unsigned),
            "sha256",
        ),
    )


def _verify_private_proof_material(
    target: _WindowsPrivateAccessEvidence,
    secret: _WindowsDeviceSecretAuthority,
    proof: WindowsPrivateProof,
    context: PrivateProofContext,
) -> None:
    try:
        target._reprove_details()
        secret_bytes = secret._secret_bytes()
        expected = _expected_windows_private_proof(target, secret_bytes, context)
        fields = (
            "owner_sid_sha256",
            "authority_descriptor_sha256",
            "owner_context_sha256",
            "device_key_id",
            "device_secret_mac",
        )
        if (
            proof.schema != expected.schema
            or proof.object_role is not expected.object_role
            or proof.security_profile_id != expected.security_profile_id
            or any(
                not hmac.compare_digest(getattr(proof, name), getattr(expected, name))
                for name in fields
            )
        ):
            raise _private_unproven()
    except PlatformFileError:
        raise _private_unproven() from None
    except Exception:
        raise _private_unproven() from None


class WindowsPlatformAdapter(
    WindowsRootedFileSystem,
    WindowsProcessFileLock,
    PrivateStorageProof,
    PersistentPrivateProof,
):
    """Composed Windows rooted, lock, private-proof, and publish backend."""

    def __init__(
        self,
        *,
        _api: WindowsFileAPI | None = None,
        _fault_injector: _FaultInjector | None = None,
    ) -> None:
        WindowsRootedFileSystem.__init__(
            self,
            _api=_api,
            _fault_injector=_fault_injector,
        )

    def _create_private_directory(
        self,
        parent: BoundDirectoryAuthority,
        name: str,
    ) -> BoundDirectoryAuthority:
        if type(parent) is not _WindowsBoundDirectory:
            raise _private_unproven()
        _validate_windows_component(
            name,
            maximum_units=parent._maximum_component_units,
        )
        records: tuple[_WindowsDirectoryRecord, ...] = ()
        created = False
        try:
            parent._reprove()
            path = _append_component(
                parent._leaf_path,
                name,
                maximum_units=parent._maximum_component_units,
            )
            user_sid = _private_primary_user_sid(parent._api)
            with _private_creation_security(parent._api, user_sid) as security:
                parent._api.checked_bool(
                    "CreateDirectoryW",
                    parent._api.CreateDirectoryW,
                    path,
                    ctypes.byref(security),
                )
            created = True
            records = _duplicate_directory_chain(parent._api, parent._records)
            records = _open_directory_components(
                parent._api,
                records,
                (name,),
                maximum_component_units=parent._maximum_component_units,
            )
            security_handle = _open_private_security_handle(
                parent._api,
                records[-1].expected_final_path,
                kind="directory",
            )
            try:
                with security_handle.borrow() as raw:
                    proof = _capture_handle_proof(
                        parent._api,
                        raw,
                        expected_final_path=records[-1].expected_final_path,
                        expected_kind="directory",
                        stale=True,
                    )
                    facts = _private_security_facts(parent._api, raw)
                if proof.identity != records[-1].identity:
                    raise _private_unproven()
            finally:
                security_handle.close()
            if facts.owner_sid != user_sid:
                raise _private_unproven()
            transferred = records
            records = ()
            return _WindowsBoundDirectory(
                parent._api,
                parent._issuer,
                parent._root_anchor,
                transferred,
                parent._maximum_component_units,
                self._fault_injector,
            )
        except PlatformFileError as error:
            if created:
                raise _recovery_required() from None
            if error.code == PlatformFileErrorCode.OUTSIDE_ROOT.value:
                raise
            raise _private_unproven() from None
        except Exception:
            if created:
                raise _recovery_required() from None
            raise _private_unproven() from None
        finally:
            if records:
                _close_handles_reverse(tuple(record.handle for record in records))

    def _prove_private(
        self,
        authority: BoundDirectoryAuthority | BoundRegularFile,
    ) -> PrivateAccessEvidence:
        return _private_evidence_from_authority(self, authority)

    def _bind_device_secret(
        self,
        secret_file: BoundRegularFile,
    ) -> DeviceSecretAuthority:
        if type(secret_file) is not _WindowsBoundRegularFile:
            raise _private_unproven()
        evidence = _private_evidence_from_authority(self, secret_file)
        try:
            return _WindowsDeviceSecretAuthority(self, evidence)
        except BaseException:
            evidence.close()
            raise

    def _mint(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        context: PrivateProofContext,
    ) -> WindowsPrivateProof:
        if (
            type(target) is not _WindowsPrivateAccessEvidence
            or type(secret) is not _WindowsDeviceSecretAuthority
            or not target._issued_by(self)
            or not secret._issued_by(self)
        ):
            raise _private_unproven()
        try:
            target._reprove_details()
            return _expected_windows_private_proof(
                target,
                secret._secret_bytes(),
                context,
            )
        except PlatformFileError:
            raise _private_unproven() from None
        except Exception:
            raise _private_unproven() from None

    def _verify(
        self,
        target: PrivateAccessEvidence,
        secret: DeviceSecretAuthority,
        proof: WindowsPrivateProof,
        expected_context: PrivateProofContext,
    ) -> VerifiedPrivateProof:
        if (
            type(target) is not _WindowsPrivateAccessEvidence
            or type(secret) is not _WindowsDeviceSecretAuthority
            or not target._issued_by(self)
            or not secret._issued_by(self)
        ):
            raise _private_unproven()
        _verify_private_proof_material(target, secret, proof, expected_context)
        retained_target = None
        retained_secret = None
        try:
            retained_target = target._clone()
            retained_secret = secret._clone()
            result = _WindowsVerifiedPrivateProof(
                self,
                retained_target,
                retained_secret,
                proof,
                expected_context,
            )
            retained_target = None
            retained_secret = None
            return result
        except PlatformFileError:
            raise _private_unproven() from None
        except Exception:
            raise _private_unproven() from None
        finally:
            if retained_target is not None:
                retained_target.close()
            if retained_secret is not None:
                retained_secret.close()

    def _consume_verified(
        self,
        verified: VerifiedPrivateProof,
        expected_context: PrivateProofContext,
    ) -> None:
        if (
            type(verified) is not _WindowsVerifiedPrivateProof
            or not verified._matches(self, expected_context)
            or verified.closed
        ):
            raise _private_unproven()
        try:
            VerifiedPrivateProof._consume_once(
                verified,
                verified._terminal_reproof,
            )
        except PlatformFileError:
            raise _private_unproven() from None
        except Exception:
            raise _private_unproven() from None
