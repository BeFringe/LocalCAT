"""Collect an optional system-file diagnostic snapshot, never an allowlist.

This independent diagnostic records observed files rather than retained proof.
The legacy --candidate-contract option only reads a historical v2 target;
current diagnostics use an independent --target, never the production contract.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


class SystemProfileError(ValueError):
    pass


ProfileError = SystemProfileError


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")).hexdigest()


def parse_json(raw: str) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ProfileError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ProfileError(f"nonfinite JSON value: {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProfileError("invalid JSON") from exc


def _keys(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ProfileError(f"unexpected {label} fields")


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProfileError(f"invalid {label}")


def _basename(value):
    if type(value) is not str or re.fullmatch(r"[a-z0-9_-]+\.dll", value) is None:
        raise ProfileError("DLL names must be canonical lowercase basenames")


def validate_target(target: Any) -> None:
    _keys(target, ("schema", "phase", "trigger", "members"), "target")
    _keys(target["trigger"], ("dll", "symbol", "hAlgorithm", "flags"), "trigger")
    expected = {
        "schema": "localcat.windows-system-provider-target.v1",
        "phase": "pre-E10",
        "trigger": {"dll": "bcrypt.dll", "symbol": "BCryptGenRandom", "hAlgorithm": 0, "flags": 2},
        "members": [{"role": "api_host", "basename": "bcrypt.dll"}, {"role": "provider", "basename": "bcryptprimitives.dll"}],
    }
    # Canonical JSON also distinguishes False from zero and floats from ints.
    if canonical_digest(target) != canonical_digest(expected):
        raise ProfileError("target differs from exact v1 phase/API/arguments/members")


def _imports(value):
    if type(value) is not list:
        raise ProfileError("imports must be a list")
    names = []
    for item in value:
        _keys(item, ("dll", "symbols"), "import")
        _basename(item["dll"])
        names.append(item["dll"])
        symbols = item["symbols"]
        if type(symbols) is not list or not symbols or any(type(symbol) is not str or not symbol or not symbol.isascii() for symbol in symbols):
            raise ProfileError("invalid import symbols")
        if symbols != sorted(set(symbols)):
            raise ProfileError("duplicate or unordered import symbols")
    if names != sorted(set(names)):
        raise ProfileError("duplicate or unordered DLL imports")


def validate_profile(profile: Any, target: Any, *, expected: Any = None) -> None:
    """Pure shape/binding validation; expected additionally requires exact replay."""
    validate_target(target)
    _keys(profile, ("schema", "scope", "target_sha256", "windows", "members"), "profile")
    if profile["schema"] != "localcat.windows-system-provider-profile.v1" or profile["scope"] != "explicit-system-roots":
        raise ProfileError("unknown profile schema or scope")
    if profile["target_sha256"] != canonical_digest(target):
        raise ProfileError("profile target digest mismatch")
    host = profile["windows"]
    _keys(host, ("major", "minor", "build", "ubr", "architecture"), "Windows")
    for key in ("major", "minor", "build", "ubr"):
        _integer(host[key], 0, 0xffffffff, f"Windows {key}")
    if host["major"] != 10 or host["build"] == 0 or host["architecture"] != "AMD64":
        raise ProfileError("unsupported Windows host")
    if type(profile["members"]) is not list or len(profile["members"]) != 2:
        raise ProfileError("explicit member count mismatch")
    for item, root in zip(profile["members"], target["members"]):
        _keys(item, ("role", "basename", "size", "sha256", "file_version", "pe"), "member")
        if {key: item[key] for key in root} != root:
            raise ProfileError("provider role/basename mismatch")
        _integer(item["size"], 1, 0xffffffff, "file size")
        if type(item["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None:
            raise ProfileError("invalid member digest")
        if type(item["file_version"]) is not str or re.fullmatch(r"\d+\.\d+\.\d+\.\d+", item["file_version"]) is None:
            raise ProfileError("invalid file version")
        pe = item["pe"]
        _keys(pe, ("machine", "static_imports", "delay_imports", "exports"), "PE")
        if type(pe["machine"]) is not int or pe["machine"] != 0x8664:
            raise ProfileError("non-AMD64 PE")
        _imports(pe["static_imports"])
        _imports(pe["delay_imports"])
        exports = pe["exports"]
        if type(exports) is not list or not exports:
            raise ProfileError("missing exports")
        identities = []
        names = []
        for symbol in exports:
            _keys(symbol, ("ordinal", "name", "rva", "forwarder"), "export")
            _integer(symbol["ordinal"], 1, 0xffffffff, "export ordinal")
            _integer(symbol["rva"], 0, 0xffffffff, "export RVA")
            for key in ("name", "forwarder"):
                value = symbol[key]
                if value is not None and (type(value) is not str or not value or not value.isascii()):
                    raise ProfileError(f"invalid export {key}")
            identities.append((symbol["ordinal"], symbol["name"] or ""))
            if symbol["name"] is not None:
                names.append(symbol["name"])
        if identities != sorted(set(identities)) or len(names) != len(set(names)):
            raise ProfileError("duplicate or unordered exports")
        if item["role"] == "api_host" and "BCryptGenRandom" not in names:
            raise ProfileError("API host lacks required export")
    if expected is not None:
        validate_profile(expected, target)
        if canonical_digest(profile) != canonical_digest(expected):
            raise ProfileError("materialized system profile replay mismatch")


def _pe_projection(data: bytes) -> tuple[str, dict]:
    import pefile  # Only the Windows collector needs this pinned build dependency.

    def imports(pe, attribute):
        rows = []
        for descriptor in getattr(pe, attribute, []):
            symbols = [symbol.name.decode("ascii") if symbol.name is not None else f"#{symbol.ordinal}" for symbol in descriptor.imports]
            rows.append({"dll": descriptor.dll.decode("ascii").lower(), "symbols": sorted(symbols)})
        return sorted(rows, key=lambda row: row["dll"])

    try:
        with pefile.PE(data=data, fast_load=False) as pe:
            if pe.get_warnings():
                raise ProfileError("PE parser warnings prevent exact inventory")
            version = getattr(pe, "VS_FIXEDFILEINFO", [])
            if len(version) != 1 or not (pe.FILE_HEADER.Characteristics & 0x2000):
                raise ProfileError("expected versioned DLL")
            version = version[0]
            file_version = ".".join(str(value) for value in (version.FileVersionMS >> 16, version.FileVersionMS & 0xffff, version.FileVersionLS >> 16, version.FileVersionLS & 0xffff))
            exports = [{"ordinal": entry.ordinal, "name": entry.name.decode("ascii") if entry.name is not None else None, "rva": entry.address,
                        "forwarder": entry.forwarder.decode("ascii") if entry.forwarder is not None else None}
                       for entry in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])]
            projection = {"machine": pe.FILE_HEADER.Machine, "static_imports": imports(pe, "DIRECTORY_ENTRY_IMPORT"),
                          "delay_imports": imports(pe, "DIRECTORY_ENTRY_DELAY_IMPORT"),
                          "exports": sorted(exports, key=lambda item: (item["ordinal"], item["name"] or ""))}
            # A nonempty directory silently omitted by the parser is not empty evidence.
            for index, attribute in ((0, "DIRECTORY_ENTRY_EXPORT"), (1, "DIRECTORY_ENTRY_IMPORT"), (13, "DIRECTORY_ENTRY_DELAY_IMPORT")):
                directory = pe.OPTIONAL_HEADER.DATA_DIRECTORY[index]
                if (directory.VirtualAddress or directory.Size) and not hasattr(pe, attribute):
                    raise ProfileError("unparsed nonempty PE directory")
            return file_version, projection
    except (pefile.PEFormatError, UnicodeError, IndexError, AttributeError) as exc:
        raise ProfileError("invalid system PE") from exc


def _windows_context() -> tuple[dict, Path]:
    if os.name != "nt":
        raise ProfileError("system profile collection requires Windows x64")
    import winreg
    from ctypes import wintypes

    class Version(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("major", wintypes.DWORD), ("minor", wintypes.DWORD),
                    ("build", wintypes.DWORD), ("platform", wintypes.DWORD), ("service_pack", wintypes.WCHAR * 128)]

    native = ctypes.WinDLL("ntdll", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    native.RtlGetVersion.argtypes = [ctypes.POINTER(Version)]
    native.RtlGetVersion.restype = wintypes.LONG
    version = Version(); version.size = ctypes.sizeof(version)
    if native.RtlGetVersion(ctypes.byref(version)) != 0:
        raise ProfileError("RtlGetVersion failed")
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT), ctypes.POINTER(wintypes.USHORT)]
    kernel.IsWow64Process2.restype = wintypes.BOOL
    process, machine = wintypes.USHORT(), wintypes.USHORT()
    if not kernel.IsWow64Process2(kernel.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(machine)) or process.value != 0 or machine.value != 0x8664 or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ProfileError("collector must run as native Windows AMD64")
    kernel.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    kernel.GetSystemDirectoryW.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise ProfileError("GetSystemDirectoryW failed")
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        ubr, kind = winreg.QueryValueEx(key, "UBR")
        if kind != winreg.REG_DWORD:
            raise ProfileError("Windows UBR has unexpected type")
    return {"major": version.major, "minor": version.minor, "build": version.build, "ubr": ubr, "architecture": "AMD64"}, Path(buffer.value)


def collect_profile(target: Any) -> dict:
    """Capture explicit roots from OS System32; never execute their exports."""
    validate_target(target)
    host, system32 = _windows_context()
    members = []
    for root in target["members"]:
        path = system32 / root["basename"]
        # One exact read supplies both digest and PE parsing. This is materialization,
        # not runtime file locking or a claim about an already loaded module.
        data = path.read_bytes()
        version, pe = _pe_projection(data)
        members.append({**root, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "file_version": version, "pe": pe})
    if _windows_context() != (host, system32):
        raise ProfileError("Windows context changed during materialization")
    result = {"schema": "localcat.windows-system-provider-profile.v1", "scope": "explicit-system-roots",
              "target_sha256": canonical_digest(target), "windows": host, "members": members}
    validate_profile(result, target)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--target", type=Path)
    source.add_argument("--candidate-contract", type=Path, help="historical v2 diagnostic only")
    parser.add_argument("--expected-profile", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        document = parse_json((args.target or args.candidate_contract).read_text(encoding="utf-8"))
        if args.candidate_contract:
            try:
                target = document["dynamic_loader"]["system_provider_target"]
            except (KeyError, TypeError) as exc:
                raise ProfileError("candidate contract lacks system_provider_target") from exc
        else:
            target = document
        result = collect_profile(target)
        if args.expected_profile:
            validate_profile(result, target, expected=parse_json(args.expected_profile.read_text(encoding="utf-8")))
        with args.output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
        print("MATERIALIZED_EXPLICIT_SYSTEM_ROOTS")
        return 0
    except (ProfileError, OSError) as exc:
        parser.exit(1, f"system profile: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
