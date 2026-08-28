"""Task 3.3 validation-only native helper build and PE evidence."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from typing import Any


HELPER_MAGIC = 0x5441434C
HELPER_VERSION = 2
MAX_SID_BYTES = 68
MAX_RESTRICTED_SIDS = 16


class NativeSidSlot(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("bytes", ctypes.c_ubyte * MAX_SID_BYTES),
    ]


class NativeOperationResult(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("status", ctypes.c_uint32),
        ("phase", ctypes.c_uint32),
        ("winerror", ctypes.c_uint32),
        ("transferred", ctypes.c_uint32),
    ]


class NativeEvidenceRecord(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ("magic", ctypes.c_uint32),
        ("version", ctypes.c_uint32),
        ("record_size", ctypes.c_uint32),
        ("helper_status", ctypes.c_uint32),
        ("helper_winerror", ctypes.c_uint32),
        ("token_type", ctypes.c_uint32),
        ("elevation_type", ctypes.c_uint32),
        ("integrity_rid", ctypes.c_uint32),
        ("is_restricted", ctypes.c_uint32),
        ("sandbox_inert", ctypes.c_uint32),
        ("has_restrictions", ctypes.c_uint32),
        ("user_sid_length", ctypes.c_uint32),
        ("integrity_sid_length", ctypes.c_uint32),
        ("restricted_sid_count", ctypes.c_uint32),
        ("user_sid", ctypes.c_ubyte * MAX_SID_BYTES),
        ("integrity_sid", ctypes.c_ubyte * MAX_SID_BYTES),
        ("restricted_sids", NativeSidSlot * MAX_RESTRICTED_SIDS),
        ("read_result", NativeOperationResult),
        ("open_result", NativeOperationResult),
        ("write_result", NativeOperationResult),
        ("delete_result", NativeOperationResult),
    ]


@dataclass(frozen=True)
class NativeHelperBuild:
    executable: Path
    evidence: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_hash(command: list[str]) -> str:
    encoded = json.dumps(command, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _version_key(name: str) -> tuple[int, ...] | None:
    try:
        parts = tuple(int(part) for part in name.split("."))
    except ValueError:
        return None
    return parts if len(parts) >= 3 else None


def _latest_complete_version(root: Path, predicate: object) -> Path:
    candidates = []
    for candidate in root.iterdir():
        key = _version_key(candidate.name)
        if candidate.is_dir() and key is not None and predicate(candidate):
            candidates.append((key, candidate))
    if not candidates:
        raise RuntimeError(f"no complete installed toolchain version under {root}")
    return max(candidates)[1]


def _discover_toolchain() -> dict[str, Path | str]:
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("native helper requires Windows x64")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if not program_files_x86:
        raise RuntimeError("ProgramFiles(x86) is unavailable")
    vswhere = (
        Path(program_files_x86)
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe"
    )
    if not vswhere.is_file():
        raise RuntimeError("vswhere.exe is unavailable")
    result = subprocess.run(
        [
            str(vswhere),
            "-latest",
            "-products",
            "*",
            "-requires",
            "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-property",
            "installationPath",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("supported MSVC x64 installation is unavailable")
    installation = Path(result.stdout.strip().splitlines()[-1])
    msvc = _latest_complete_version(
        installation / "VC" / "Tools" / "MSVC",
        lambda candidate: (
            candidate / "bin" / "Hostx64" / "x64" / "cl.exe"
        ).is_file()
        and (
            candidate / "bin" / "Hostx64" / "x64" / "link.exe"
        ).is_file(),
    )

    import winreg

    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Windows Kits\Installed Roots",
        0,
        winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
    ) as key:
        kits_root = Path(winreg.QueryValueEx(key, "KitsRoot10")[0])
    sdk = _latest_complete_version(
        kits_root / "Include",
        lambda candidate: (candidate / "um" / "Windows.h").is_file()
        and (candidate / "shared" / "sdkddkver.h").is_file()
        and (kits_root / "Lib" / candidate.name / "um" / "x64" / "kernel32.lib").is_file()
        and (kits_root / "Lib" / candidate.name / "um" / "x64" / "advapi32.lib").is_file(),
    )
    tools = msvc / "bin" / "Hostx64" / "x64"
    libraries = kits_root / "Lib" / sdk.name / "um" / "x64"
    return {
        "vswhere": vswhere,
        "installation": installation,
        "msvc_version": msvc.name,
        "sdk_version": sdk.name,
        "cl": tools / "cl.exe",
        "link": tools / "link.exe",
        "msvc_include": msvc / "include",
        "windows_h": sdk / "um" / "Windows.h",
        "shared_include": sdk / "shared",
        "um_include": sdk / "um",
        "ucrt_include": sdk / "ucrt",
        "kernel32_lib": libraries / "kernel32.lib",
        "advapi32_lib": libraries / "advapi32.lib",
    }


def _rva_to_offset(
    rva: int,
    sections: list[tuple[int, int, int, int]],
) -> int:
    for virtual_address, virtual_size, raw_offset, raw_size in sections:
        extent = max(virtual_size, raw_size)
        if virtual_address <= rva < virtual_address + extent:
            return raw_offset + (rva - virtual_address)
    raise RuntimeError(f"PE RVA is outside materialized sections: {rva:#x}")


def _read_c_string(data: bytes, offset: int) -> str:
    end = data.find(b"\0", offset)
    if end < 0:
        raise RuntimeError("unterminated PE import name")
    return data[offset:end].decode("ascii")


def inspect_pe_imports(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    if len(data) < 0x100 or data[:2] != b"MZ":
        raise RuntimeError("helper is not a PE image")
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise RuntimeError("helper PE signature is invalid")
    machine, section_count = struct.unpack_from("<HH", data, pe_offset + 4)
    optional_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    optional_offset = pe_offset + 24
    if machine != 0x8664 or struct.unpack_from("<H", data, optional_offset)[0] != 0x20B:
        raise RuntimeError("helper is not a PE32+ AMD64 image")
    directory_offset = optional_offset + 112
    import_rva, import_size = struct.unpack_from("<II", data, directory_offset + 8)
    bound_rva, bound_size = struct.unpack_from("<II", data, directory_offset + 11 * 8)
    delay_rva, delay_size = struct.unpack_from("<II", data, directory_offset + 13 * 8)
    section_offset = optional_offset + optional_size
    sections = []
    for index in range(section_count):
        entry = section_offset + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from(
            "<IIII", data, entry + 8
        )
        sections.append((virtual_address, virtual_size, raw_offset, raw_size))
    if import_rva == 0 or import_size == 0:
        raise RuntimeError("helper has no auditable import directory")
    descriptors = _rva_to_offset(import_rva, sections)
    imports: dict[str, tuple[str, ...]] = {}
    for descriptor_index in range(64):
        descriptor = descriptors + descriptor_index * 20
        original_thunk, _, _, name_rva, first_thunk = struct.unpack_from(
            "<IIIII", data, descriptor
        )
        if not any((original_thunk, name_rva, first_thunk)):
            break
        dll = _read_c_string(data, _rva_to_offset(name_rva, sections)).upper()
        thunk_rva = original_thunk or first_thunk
        thunk_offset = _rva_to_offset(thunk_rva, sections)
        symbols = []
        for thunk_index in range(256):
            thunk = struct.unpack_from("<Q", data, thunk_offset + thunk_index * 8)[0]
            if thunk == 0:
                break
            if thunk & (1 << 63):
                symbols.append(f"ordinal:{thunk & 0xFFFF}")
            else:
                symbol_offset = _rva_to_offset(int(thunk), sections)
                symbols.append(_read_c_string(data, symbol_offset + 2))
        else:
            raise RuntimeError("helper import thunk table is not closed")
        imports[dll] = tuple(symbols)
    else:
        raise RuntimeError("helper import descriptor table is not closed")
    return {
        "machine": machine,
        "imports": imports,
        "bound_import_directory": (bound_rva, bound_size),
        "delay_import_directory": (delay_rva, delay_size),
    }


def build_native_helper(source: Path, output_directory: Path) -> NativeHelperBuild:
    source = source.resolve(strict=True)
    output_directory = output_directory.resolve()
    toolchain = _discover_toolchain()
    output_directory.mkdir(parents=True, exist_ok=True)
    object_path = output_directory / "windows_lock_access_helper.obj"
    executable = output_directory / "windows_lock_access_helper.exe"
    compile_command = [
        str(toolchain["cl"]),
        "/nologo",
        "/c",
        "/TC",
        "/W4",
        "/WX",
        "/Od",
        "/GS-",
        "/guard:cf-",
        "/utf-8",
        f"/I{toolchain['msvc_include']}",
        f"/I{toolchain['shared_include']}",
        f"/I{toolchain['um_include']}",
        f"/I{toolchain['ucrt_include']}",
        f"/Fo{object_path}",
        str(source),
    ]
    link_command = [
        str(toolchain["link"]),
        "/nologo",
        "/machine:x64",
        "/subsystem:windows",
        "/entry:LocalCatTestEntry",
        "/nodefaultlib",
        "/incremental:no",
        "/manifest:no",
        "/dynamicbase",
        "/nxcompat",
        "/opt:ref",
        "/opt:icf",
        f"/out:{executable}",
        str(object_path),
        str(toolchain["kernel32_lib"]),
        str(toolchain["advapi32_lib"]),
    ]
    results = []
    for command in (compile_command, link_command):
        result = subprocess.run(
            command,
            cwd=output_directory,
            capture_output=True,
            check=False,
            text=True,
            timeout=60,
        )
        results.append(result)
        if result.returncode != 0:
            raise RuntimeError(
                json.dumps(
                    {
                        "command": command,
                        "returncode": result.returncode,
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
    pe = inspect_pe_imports(executable)
    evidence: dict[str, Any] = {
        "source": str(source.resolve()),
        "source_sha256": _sha256(source),
        "compiler": str(Path(toolchain["cl"]).resolve()),
        "compiler_sha256": _sha256(Path(toolchain["cl"])),
        "linker": str(Path(toolchain["link"]).resolve()),
        "linker_sha256": _sha256(Path(toolchain["link"])),
        "msvc_version": toolchain["msvc_version"],
        "sdk_version": toolchain["sdk_version"],
        "sdk_windows_h_sha256": _sha256(Path(toolchain["windows_h"])),
        "sdk_kernel32_lib_sha256": _sha256(Path(toolchain["kernel32_lib"])),
        "sdk_advapi32_lib_sha256": _sha256(Path(toolchain["advapi32_lib"])),
        "compile_command": compile_command,
        "compile_command_sha256": _command_hash(compile_command),
        "compile_returncode": results[0].returncode,
        "link_command": link_command,
        "link_command_sha256": _command_hash(link_command),
        "link_returncode": results[1].returncode,
        "binary_sha256": _sha256(executable),
        "pe": pe,
    }
    return NativeHelperBuild(executable=executable, evidence=evidence)


def decode_native_evidence(raw: bytes) -> NativeEvidenceRecord:
    if len(raw) != ctypes.sizeof(NativeEvidenceRecord):
        raise RuntimeError(
            f"native evidence size mismatch: {len(raw)} != {ctypes.sizeof(NativeEvidenceRecord)}"
        )
    record = NativeEvidenceRecord.from_buffer_copy(raw)
    if (
        record.magic != HELPER_MAGIC
        or record.version != HELPER_VERSION
        or record.record_size != ctypes.sizeof(NativeEvidenceRecord)
    ):
        raise RuntimeError("native evidence envelope mismatch")
    return record
