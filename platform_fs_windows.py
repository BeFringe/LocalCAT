"""Windows native/host fact probe for the platform file adapter.

Task 3.1 exposes native/host facts.  Task 3.2 adds rooted read authorities;
locks, private proof, publication, and durability capability remain later work.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from pathlib import Path, PurePath
import struct
import sys
from typing import Callable

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateFile,
    EntrySnapshot,
    FileObjectIdentity,
    LockLease,
    PendingPublication,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishFacts,
    PublishMode,
    RootedDirectoryAuthority,
    RootedFileSystem,
)
from windows_file_api import (
    DWORD,
    FILE_BASIC_INFO,
    FILE_ATTRIBUTE_TAG_INFO,
    FILE_ID_INFO,
    FILE_STANDARD_INFO,
    LARGE_INTEGER,
    STORAGE_DESCRIPTOR_HEADER,
    STORAGE_DEVICE_DESCRIPTOR,
    STORAGE_PROPERTY_QUERY,
    STORAGE_WRITE_CACHE_PROPERTY,
    WORD,
    Win32CallError,
    WindowsFileAPI,
)


__all__ = [
    "WindowsHostFacts",
    "WindowsNativeFacts",
    "WindowsRootedFileSystem",
    "probe_windows_host_facts",
    "probe_windows_native_facts",
]


FILE_READ_ATTRIBUTES = 0x0080
FILE_LIST_DIRECTORY = 0x0001
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_PERSISTENT_ACLS = 0x00000008
FILE_SUPPORTS_REPARSE_POINTS = 0x00000080

FILE_BASIC_INFO_CLASS = 0
FILE_STANDARD_INFO_CLASS = 1
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_ID_INFO_CLASS = 18
VOLUME_NAME_GUID = 0x1

DRIVE_FIXED = 3
VER_NT_WORKSTATION = 1
WINDOWS_11_MINIMUM_BUILD = 22000
IMAGE_FILE_MACHINE_UNKNOWN = 0x0000
IMAGE_FILE_MACHINE_AMD64 = 0x8664

IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
STORAGE_DEVICE_PROPERTY = 0
STORAGE_DEVICE_WRITE_CACHE_PROPERTY = 4
PROPERTY_STANDARD_QUERY = 0
STORAGE_BUS_TYPE_UNKNOWN = 0
WRITE_CACHE_TYPE_UNKNOWN = 0
WRITE_CACHE_ENABLE_UNKNOWN = 0
WRITE_CACHE_CHANGE_UNKNOWN = 0
WRITE_THROUGH_UNKNOWN = 0
STORAGE_BUS_TYPE_MAX = 20
WRITE_CACHE_TYPE_MAX = 4
WRITE_CACHE_ENABLE_MAX = 3
WRITE_CACHE_CHANGE_MAX = 3
WRITE_THROUGH_MAX = 3

_MAX_PATH_BUFFER = 32768
_MAX_STORAGE_DESCRIPTOR = 1024 * 1024
_MAX_EXTENDED_PATH_UTF16_UNITS = 32767
_READ_CHUNK_BYTES = 64 * 1024
FILE_BEGIN = 0

ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32


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


@dataclass(frozen=True, slots=True)
class WindowsNativeFacts:
    """Strict host plus storage observations; this does not grant authority."""

    host: WindowsHostFacts
    storage_bus_type: int
    write_cache_type: int
    write_cache_enabled: int
    write_cache_changeable: int
    write_through_supported: int
    flush_cache_supported: int
    user_defined_power_protection: int
    nv_cache_enabled: int


def _capability_unavailable() -> PlatformFileError:
    return PlatformFileError(
        PlatformFileErrorCode.CAPABILITY_UNAVAILABLE,
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


def _device_io_control(
    api: WindowsFileAPI,
    raw_handle: int,
    query: STORAGE_PROPERTY_QUERY,
    output: object,
    output_size: int,
) -> int:
    returned = DWORD()
    api.checked_bool(
        "DeviceIoControl",
        api.DeviceIoControl,
        raw_handle,
        IOCTL_STORAGE_QUERY_PROPERTY,
        ctypes.byref(query),
        ctypes.sizeof(query),
        output,
        output_size,
        ctypes.byref(returned),
        None,
    )
    return int(returned.value)


def _storage_query(
    api: WindowsFileAPI,
    raw_handle: int,
    property_id: int,
    output_type: type[ctypes.Structure],
    *,
    exact_size: bool,
) -> ctypes.Structure:
    query = STORAGE_PROPERTY_QUERY(
        PropertyId=property_id,
        QueryType=PROPERTY_STANDARD_QUERY,
    )
    header = STORAGE_DESCRIPTOR_HEADER()
    header_returned = _device_io_control(
        api,
        raw_handle,
        query,
        ctypes.byref(header),
        ctypes.sizeof(header),
    )
    expected_version = ctypes.sizeof(output_type)
    size = int(header.Size)
    if (
        header_returned != ctypes.sizeof(header)
        or int(header.Version) != expected_version
        or size < expected_version
        or size > _MAX_STORAGE_DESCRIPTOR
        or (exact_size and size != expected_version)
    ):
        raise _durability_unavailable()

    buffer = ctypes.create_string_buffer(size)
    full_returned = _device_io_control(
        api,
        raw_handle,
        query,
        buffer,
        size,
    )
    if full_returned != size:
        raise _durability_unavailable()
    repeated = STORAGE_DESCRIPTOR_HEADER.from_buffer_copy(buffer.raw)
    if repeated.Version != header.Version or repeated.Size != header.Size:
        raise _durability_unavailable()
    result = output_type.from_buffer_copy(buffer.raw)
    if (
        int(result.Version) != expected_version
        or int(result.Size) != size
        or (exact_size and int(result.Size) != expected_version)
    ):
        raise _durability_unavailable()
    return result


def _storage_facts(
    api: WindowsFileAPI,
    raw_handle: int,
) -> tuple[int, STORAGE_WRITE_CACHE_PROPERTY]:
    device = _storage_query(
        api,
        raw_handle,
        STORAGE_DEVICE_PROPERTY,
        STORAGE_DEVICE_DESCRIPTOR,
        exact_size=False,
    )
    cache = _storage_query(
        api,
        raw_handle,
        STORAGE_DEVICE_WRITE_CACHE_PROPERTY,
        STORAGE_WRITE_CACHE_PROPERTY,
        exact_size=True,
    )
    if not isinstance(device, STORAGE_DEVICE_DESCRIPTOR) or not isinstance(
        cache,
        STORAGE_WRITE_CACHE_PROPERTY,
    ):
        raise _durability_unavailable()
    _validate_storage_facts(device, cache)
    return int(device.BusType), cache


def _validate_storage_facts(
    device: STORAGE_DEVICE_DESCRIPTOR,
    cache: STORAGE_WRITE_CACHE_PROPERTY,
) -> None:
    if not (
        device.Version == ctypes.sizeof(STORAGE_DEVICE_DESCRIPTOR)
        and device.Size >= device.Version
        and 0 < device.BusType < STORAGE_BUS_TYPE_MAX
        and cache.Version == ctypes.sizeof(STORAGE_WRITE_CACHE_PROPERTY)
        and cache.Size == cache.Version
        and 0 < cache.WriteCacheType < WRITE_CACHE_TYPE_MAX
        and 0 < cache.WriteCacheEnabled < WRITE_CACHE_ENABLE_MAX
        and 0 < cache.WriteCacheChangeable < WRITE_CACHE_CHANGE_MAX
        and 0 < cache.WriteThroughSupported < WRITE_THROUGH_MAX
        and cache.FlushCacheSupported in {0, 1}
        and cache.UserDefinedPowerProtection in {0, 1}
        and cache.NVCacheEnabled in {0, 1}
    ):
        raise _durability_unavailable()
    if (
        device.BusType == STORAGE_BUS_TYPE_UNKNOWN
        or cache.WriteCacheType == WRITE_CACHE_TYPE_UNKNOWN
        or cache.WriteCacheEnabled == WRITE_CACHE_ENABLE_UNKNOWN
        or cache.WriteCacheChangeable == WRITE_CACHE_CHANGE_UNKNOWN
        or cache.WriteThroughSupported == WRITE_THROUGH_UNKNOWN
    ):
        raise _durability_unavailable()


def _validate_probe_root(probe_root: Path) -> None:
    if sys.platform != "win32":
        raise _capability_unavailable()
    if not isinstance(probe_root, Path) or not probe_root.is_absolute():
        raise _capability_unavailable()
    if probe_root.drive.startswith("\\") or len(probe_root.drive) != 2:
        raise _capability_unavailable()


def _probe_windows(
    probe_root: Path,
    *,
    include_storage: bool,
) -> tuple[WindowsHostFacts, tuple[int, STORAGE_WRITE_CACHE_PROPERTY] | None]:
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
                    storage: tuple[int, STORAGE_WRITE_CACHE_PROPERTY] | None = None
                    if include_storage:
                        try:
                            with api.open_handle(
                                guid_root.rstrip("\\"),
                                desired_access=0,
                                share_mode=(
                                    FILE_SHARE_READ
                                    | FILE_SHARE_WRITE
                                    | FILE_SHARE_DELETE
                                ),
                                creation_disposition=OPEN_EXISTING,
                                flags=0,
                            ) as volume_device_handle:
                                with volume_device_handle.borrow() as raw_volume_device:
                                    storage = _storage_facts(api, raw_volume_device)
                        except PlatformFileError as error:
                            if (
                                error.code
                                == PlatformFileErrorCode.DURABILITY_UNAVAILABLE.value
                            ):
                                raise
                            raise _durability_unavailable() from None
                        except Exception:
                            raise _durability_unavailable() from None

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
                    return facts, storage


def probe_windows_host_facts(probe_root: Path) -> WindowsHostFacts:
    """Return validated host/root facts without minting a rooted authority."""

    try:
        facts, _ = _probe_windows(probe_root, include_storage=False)
        return facts
    except PlatformFileError:
        raise
    except Exception:
        raise _capability_unavailable() from None


def probe_windows_native_facts(probe_root: Path) -> WindowsNativeFacts:
    """Probe strict host/storage facts without reading or mutating file content."""

    try:
        host, storage = _probe_windows(probe_root, include_storage=True)
        if storage is None:
            raise _durability_unavailable()
        storage_bus_type, cache = storage
        return WindowsNativeFacts(
            host=host,
            storage_bus_type=storage_bus_type,
            write_cache_type=int(cache.WriteCacheType),
            write_cache_enabled=int(cache.WriteCacheEnabled),
            write_cache_changeable=int(cache.WriteCacheChangeable),
            write_through_supported=int(cache.WriteThroughSupported),
            flush_cache_supported=int(cache.FlushCacheSupported),
            user_defined_power_protection=int(cache.UserDefinedPowerProtection),
            nv_cache_enabled=int(cache.NVCacheEnabled),
        )
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


def _open_entry_proof(
    api: WindowsFileAPI,
    path: str,
    expected_final_path: str,
    *,
    expected_kind: str | None,
    expected_volume_id: bytes,
    stale: bool,
    allow_missing: bool = False,
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
            )
            _require_volume(proof.identity, expected_volume_id, stale=stale)
            return proof
    except PlatformFileError:
        raise
    except Win32CallError as error:
        if allow_missing and error.winerror in {ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND}:
            return None
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
    "_records",
    "_maximum_component_units",
    "_fault_injector",
)


class _WindowsDirectoryAuthorityMixin:
    __slots__ = ()

    def __init__(
        self,
        api: WindowsFileAPI,
        records: tuple[_WindowsDirectoryRecord, ...],
        maximum_component_units: int,
        fault_injector: _FaultInjector | None,
    ) -> None:
        super().__init__()
        if not records:
            raise ValueError("Windows directory authority requires retained handles")
        self._api = api
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

    def _create_candidate(self, name: str, *, private: bool) -> CandidateFile:
        del private
        _validate_windows_component(name, maximum_units=self._maximum_component_units)
        raise _capability_unavailable()

    def _rename_candidate(
        self,
        candidate: CandidateFile,
        destination: str,
        *,
        mode: PublishMode,
        lease: LockLease | None,
    ) -> PublishFacts:
        del candidate, mode, lease
        _validate_windows_component(
            destination,
            maximum_units=self._maximum_component_units,
        )
        raise _capability_unavailable()

    def _open_published_destination(
        self,
        destination: str,
        preliminary_facts: PublishFacts,
    ) -> BoundRegularFile:
        del preliminary_facts
        _validate_windows_component(
            destination,
            maximum_units=self._maximum_component_units,
        )
        raise _capability_unavailable()

    def _create_pending_publication(
        self,
        preliminary_facts: PublishFacts,
        retained_destination: BoundRegularFile,
    ) -> PendingPublication:
        del preliminary_facts, retained_destination
        raise _capability_unavailable()

    def _unlink_owned(self, name: str, expected: FileObjectIdentity) -> None:
        del expected
        _validate_windows_component(name, maximum_units=self._maximum_component_units)
        raise _capability_unavailable()

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


class _WindowsBoundRegularFile(BoundRegularFile):
    __slots__ = (
        "_api",
        "_records",
        "_handle",
        "_entry_path",
        "_entry_identity",
        "_maximum_component_units",
        "_fault_injector",
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

    def _read_all(self) -> bytes:
        try:
            before = self._reprove().snapshot
            _hit_fault(self._fault_injector, "windows_before_body_read")
            chunks: list[bytes] = []
            with self._handle.borrow() as raw:
                distance = LARGE_INTEGER()
                distance.QuadPart = 0
                self._api.checked_bool(
                    "SetFilePointerEx",
                    self._api.SetFilePointerEx,
                    raw,
                    distance,
                    None,
                    FILE_BEGIN,
                )
                remaining = before.byte_count
                while remaining:
                    requested = min(_READ_CHUNK_BYTES, remaining)
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
            payload = b"".join(chunks)
            after = self._reprove().snapshot
            if before != after or len(payload) != after.byte_count:
                raise _identity_stale()
            return payload
        except PlatformFileError:
            raise
        except Exception:
            raise _identity_stale() from None

    def _identity(self) -> FileObjectIdentity:
        return self._reprove().identity

    def _snapshot(self) -> EntrySnapshot:
        return self._reprove().snapshot

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


class WindowsRootedFileSystem(RootedFileSystem):
    """Task 3.2 Windows rooted read capability; later services remain unavailable."""

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
        self._fault_injector = _fault_injector

    def _native_api(self) -> WindowsFileAPI:
        if self._api is None:
            self._api = WindowsFileAPI.load()
        return self._api

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
            )
            if probed is None:
                raise _capability_unavailable()
            _hit_fault(self._fault_injector, "windows_after_entry_probe")
            handle = root._api.open_handle(
                entry_path,
                desired_access=GENERIC_READ | FILE_READ_ATTRIBUTES | SYNCHRONIZE,
                share_mode=FILE_SHARE_READ,
                creation_disposition=OPEN_EXISTING,
                flags=FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_SEQUENTIAL_SCAN,
            )
            with handle.borrow() as raw:
                source = _capture_handle_proof(
                    root._api,
                    raw,
                    expected_final_path=entry_path,
                    expected_kind="regular",
                    stale=False,
                )
            if not _same_object(probed.identity, source.identity):
                raise _identity_stale()
            _require_volume(
                source.identity,
                records[0].identity.volume_id,
                stale=False,
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
