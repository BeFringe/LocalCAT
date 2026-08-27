"""Portable Windows release evidence writer and strict bundle validator.

The harness records verification facts; it never upgrades a failed capability.
Actual process paths remain in memory. Persisted identities use repository-relative
artifact paths and content digests only.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Any


SCHEMA_ID_V1 = "localcat.windows-release-evidence.v1"
SCHEMA_ID = "localcat.windows-release-evidence.v2"
EVENT_SCHEMA_ID = "localcat.windows-release-event.v1"
SCENARIO_CONTRACT_SCHEMA_ID_V1 = "localcat.windows-release-scenario-contract.v1"
SCENARIO_CONTRACT_SCHEMA_ID = "localcat.windows-release-scenario-contract.v2"
MANIFEST_NAME = "manifest.json"
CHECKSUM_NAME = "checksums.sha256"
EVENT_LOG_NAME = "events.jsonl"

_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_COMMAND_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE_RE = re.compile(r"[A-Z][A-Z0-9_.-]{0,127}\Z")
_SAFE_VALUE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,255}\Z")
_SCENARIO_CONTRACT_KEY_RE = re.compile(
    r"packaging/windows/evidence-scenarios/[a-z0-9][a-z0-9._-]{0,119}\.json\Z"
)
_ENVIRONMENT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_()]{0,127}\Z")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/][^\r\n\"'<>|]*)"
)
_UNC_PATH_RE = re.compile(r"(?i)(?<![\\])\\\\[^\r\n\"'<>|]+")
_POSIX_PRIVATE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])/(?:Users|home|tmp|private|var/tmp)/[^\r\n\"'<>|]*"
)

_ENVIRONMENT_KEYS = frozenset(
    {
        "os_name",
        "os_version",
        "os_build",
        "architecture",
        "python",
        "pyside6",
        "qt",
        "pyinstaller",
        "sqlite",
        "filesystem",
        "volume_class",
    }
)
_COMMAND_KEYS_V1 = frozenset(
    {
        "id",
        "shell",
        "shell_flavor",
        "shell_version",
        "command",
        "command_sha256",
        "cwd_role",
        "environment_profile",
        "environment_profile_sha256",
        "environment_projection",
        "windowed",
        "status",
        "exit_code",
        "diagnostic_code",
        "stdout_log",
        "stdout_sha256",
        "stderr_log",
        "stderr_sha256",
        "diagnostic_marker",
        "diagnostic_sha256",
    }
)
_COMMAND_KEYS = _COMMAND_KEYS_V1 | frozenset(
    {
        "shell_edition",
        "shell_process_architecture",
        "shell_selection_role",
        "shell_binary_sha256",
    }
)
_EVENT_KEYS = frozenset(
    {
        "schema",
        "sequence",
        "operation_id",
        "command_id",
        "operation",
        "profile",
        "phase",
        "result",
        "stable_code",
        "win32_code",
        "filesystem",
        "volume_class",
        "identity_comparison",
        "final_path_verdict",
        "reparse_verdict",
        "share_flags",
        "lock_range",
        "recovery_result",
    }
)
_ARTIFACT_KEYS = frozenset({"path", "kind", "bytes", "sha256"})
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "repository",
        "run",
        "environment",
        "commands",
        "events",
        "outputs",
        "artifacts",
        "scenario_contract",
    }
)
_REPOSITORY_KEYS = frozenset({"commit", "branch"})
_RUN_KEYS = frozenset({"id", "status", "artifact_key"})
_EVENT_SUMMARY_KEYS = frozenset({"path", "count", "sha256"})
_OUTPUT_KEYS = frozenset(
    {"matrix", "windows_fs_lock_checklist", "frozen_source_packaging_checklist"}
)
_OUTPUT_REFERENCE_KEYS = frozenset({"path", "sha256"})
_SCENARIO_CONTRACT_REFERENCE_KEYS = frozenset({"key", "sha256"})
_DIAGNOSTIC_KEYS = frozenset(
    {
        "schema",
        "command_id",
        "status",
        "stable_code",
        "exit_code",
        "stdout_log",
        "stderr_log",
    }
)
_DIAGNOSTIC_CODES = frozenset(
    {
        "EVIDENCE.COMMAND.INTERRUPTED",
        "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
        "EVIDENCE.COMMAND.LAUNCH_FAILED",
        "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
        "EVIDENCE.COMMAND.TIMEOUT",
        "EVIDENCE.WINDOWED.FAILURE",
        "EVIDENCE.WINDOWED.SILENT_FAILURE",
    }
)
_MATRIX_KEYS = frozenset({"schema", "scenarios"})
_SCENARIO_KEYS = frozenset(
    {
        "id",
        "required",
        "expected_observation",
        "command_ids",
        "event_operation_ids",
        "verdict",
        "stable_code",
    }
)
_SCENARIO_VERDICTS = frozenset({"PASS", "FAIL", "INTERRUPTED", "NOT_RUN"})
_SCENARIO_CONTRACT_KEYS = frozenset({"schema", "id", "scenarios"})
_CONTRACT_SCENARIO_KEYS = frozenset(
    {"id", "required", "expected_observation", "commands", "events"}
)
_COMMAND_EXPECTATION_KEYS_V1 = frozenset(
    {
        "id",
        "command_sha256",
        "shell_flavor",
        "shell_version",
        "cwd_role",
        "environment_profile",
        "environment_profile_sha256",
        "environment_projection",
        "windowed",
        "status",
        "exit_code",
        "diagnostic_code",
    }
)
_COMMAND_EXPECTATION_KEYS = _COMMAND_EXPECTATION_KEYS_V1 | frozenset(
    {
        "shell_edition",
        "shell_process_architecture",
        "shell_selection_role",
    }
)
_EVENT_EXPECTATION_KEYS = _EVENT_KEYS - frozenset({"schema", "sequence"})
_CREATE_SUSPENDED = 0x00000004
_DEFAULT_ENVIRONMENT_PROFILE = "CLEAN_WINDOWS_V2"
_WINDOWS_POWERSHELL_V2_VERSION_POLICY = "5.1"
_WINDOWS_POWERSHELL_V2_EDITION = "Desktop"
_WINDOWS_POWERSHELL_V2_ARCHITECTURE = "X64"
_WINDOWS_POWERSHELL_V2_SELECTION = "SYSTEM32_ABSOLUTE"
_SHELL_IDENTITY_MARKER = "LOCALCAT_SHELL_IDENTITY_V2|"

_ENVIRONMENT_PROFILE_DEFINITIONS: dict[str, object] = {
    "CLEAN_WINDOWS_V1": {
        "required_keys": [
            "ALLUSERSPROFILE",
            "ComSpec",
            "PATH",
            "PATHEXT",
            "ProgramData",
            "PYTHONNOUSERSITE",
            "SystemDrive",
            "SystemRoot",
            "TEMP",
            "TMP",
            "WINDIR",
        ],
        "allowed_addition_prefix": "LOCALCAT_",
        "forbidden_keys": [
            "PYTHONHOME",
            "PYTHONPATH",
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
        ],
        "path_roles": [
            "powershell-directory",
            "program-data",
            "system-drive",
            "system32",
            "windows-root",
        ],
        "required_projection": {
            "ALLUSERSPROFILE": "program-data",
            "ComSpec": "system32/cmd.exe",
            "PATH": "powershell-directory;system32;windows-root",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "ProgramData": "program-data",
            "PYTHONNOUSERSITE": "1",
            "SystemDrive": "system-drive",
            "SystemRoot": "windows-root",
            "TEMP": "temp-root",
            "TMP": "temp-root",
            "WINDIR": "windows-root",
        },
    },
    "CLEAN_WINDOWS_V2": {
        "required_keys": [
            "ALLUSERSPROFILE",
            "ComSpec",
            "PATH",
            "PATHEXT",
            "ProgramData",
            "PYTHONNOUSERSITE",
            "SystemDrive",
            "SystemRoot",
            "TEMP",
            "TMP",
            "WINDIR",
        ],
        "allowed_addition_prefix": "LOCALCAT_",
        "forbidden_keys": [
            "PYTHONHOME",
            "PYTHONPATH",
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
        ],
        "path_roles": [
            "powershell-directory",
            "program-data",
            "system-drive",
            "system32",
            "windows-root",
        ],
        "required_projection": {
            "ALLUSERSPROFILE": "program-data",
            "ComSpec": "system32/cmd.exe",
            "PATH": "powershell-directory;system32;windows-root",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "ProgramData": "program-data",
            "PYTHONNOUSERSITE": "1",
            "SystemDrive": "system-drive",
            "SystemRoot": "windows-root",
            "TEMP": "temp-root",
            "TMP": "temp-root",
            "WINDIR": "windows-root",
        },
        "shell_policy": {
            "architecture": "x64",
            "build_revision": "recorded_nonblocking",
            "edition": "Desktop",
            "flavor": "WINDOWS_POWERSHELL",
            "major": 5,
            "minor": 1,
            "selection": "system32_absolute",
        },
    },
}


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


class _SuspendedWindowsJob:
    """Start containment before the PowerShell command executes any instruction."""

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002

    def __init__(self) -> None:
        if os.name != "nt":
            raise OSError("Windows Job Objects are unavailable on this host")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32 = kernel32
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32First.restype = wintypes.BOOL
        kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ThreadEntry32)]
        kernel32.Thread32Next.restype = wintypes.BOOL
        kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenThread.restype = wintypes.HANDLE
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle: int | None = int(handle)
        self.assigned = False
        self.resumed = False
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not kernel32.SetInformationJobObject(
            handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, process: subprocess.Popen[bytes]) -> None:
        if self.handle is None:
            raise OSError("Windows Job Object is closed")
        process_handle = wintypes.HANDLE(int(process._handle))
        if not self._kernel32.AssignProcessToJobObject(self.handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())
        self.assigned = True

        snapshot = self._kernel32.CreateToolhelp32Snapshot(self._TH32CS_SNAPTHREAD, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        thread_handle: int | None = None
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            found = self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == process.pid:
                    opened = self._kernel32.OpenThread(
                        self._THREAD_SUSPEND_RESUME,
                        False,
                        entry.th32ThreadID,
                    )
                    if opened:
                        thread_handle = int(opened)
                        break
                found = self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
            if thread_handle is None:
                raise OSError("unable to locate the suspended PowerShell thread")
            previous_count = self._kernel32.ResumeThread(thread_handle)
            if previous_count == 0xFFFFFFFF:
                raise ctypes.WinError(ctypes.get_last_error())
            self.resumed = True
        finally:
            if thread_handle is not None:
                self._kernel32.CloseHandle(thread_handle)
            self._kernel32.CloseHandle(snapshot)

    def terminate(self) -> None:
        if self.handle is not None:
            if not self._kernel32.TerminateJobObject(self.handle, 1):
                error = ctypes.get_last_error()
                if error:
                    raise ctypes.WinError(error)

    def close(self) -> None:
        if getattr(self, "handle", None) is not None:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


def _stop_and_drain_process(
    process: subprocess.Popen[bytes],
    job: _SuspendedWindowsJob | None,
    *,
    drain_timeout_seconds: float = 5.0,
) -> tuple[bytes, bytes]:
    """Stop a contained or still-suspended root process without an unbounded drain."""

    if process.poll() is None:
        if job is not None and job.assigned:
            try:
                job.terminate()
            except OSError:
                # Closing a configured kill-on-close Job is the independent fallback.
                job.close()
        else:
            # Assignment failure leaves the root suspended and outside the Job.
            process.kill()
    try:
        return process.communicate(timeout=drain_timeout_seconds)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        try:
            process.wait(timeout=drain_timeout_seconds)
        except subprocess.TimeoutExpired as error:
            raise OSError("unable to stop or drain the evidence process tree") from error
        return b"", b""

_COMMAND_STATUSES = frozenset({"PASS", "FAIL", "INTERRUPTED"})
_RUN_STATUSES = frozenset({"PASS", "FAIL", "INTERRUPTED"})
_EVENT_RESULTS = frozenset(
    {"PASS", "FAIL", "INTERRUPTED", "OBSERVED", "RECOVERY_REQUIRED", "NOT_RUN"}
)
_IDENTITY_RESULTS = frozenset({"MATCH", "MISMATCH", "NOT_OBSERVED"})
_FINAL_PATH_RESULTS = frozenset({"MATCH", "MISMATCH", "NOT_OBSERVED"})
_REPARSE_RESULTS = frozenset({"CLEAR", "REJECTED", "NOT_OBSERVED"})
_RECOVERY_RESULTS = frozenset(
    {"OLD", "NEW", "RECOVERY_REQUIRED", "NOT_APPLICABLE", "NOT_OBSERVED"}
)
_SHARE_FLAGS = frozenset({"READ", "WRITE", "DELETE", "NONE"})


class EvidenceValidationError(ValueError):
    """Raised when evidence is unsafe, malformed, incomplete, or tampered."""


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise EvidenceValidationError(
            f"{label} keys mismatch: missing={missing!r}, extra={extra!r}"
        )


def _require_string(value: object, label: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise EvidenceValidationError(f"{label} must be an exact string")
    if not allow_empty and not value:
        raise EvidenceValidationError(f"{label} must not be empty")
    return value


def _require_optional_string(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_string(value, label)


def _require_integer(value: object, label: str, *, minimum: int | None = None) -> int:
    if type(value) is not int:
        raise EvidenceValidationError(f"{label} must be an exact integer")
    if minimum is not None and value < minimum:
        raise EvidenceValidationError(f"{label} must be >= {minimum}")
    return value


def _require_optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    return _require_integer(value, label)


def _require_boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise EvidenceValidationError(f"{label} must be an exact boolean")
    return value


def _require_enum(value: object, allowed: frozenset[str], label: str) -> str:
    text = _require_string(value, label)
    if text not in allowed:
        raise EvidenceValidationError(f"{label} has unsupported value {text!r}")
    return text


def _require_safe_value(value: object, label: str) -> str:
    text = _require_string(value, label)
    if _SAFE_VALUE_RE.fullmatch(text) is None:
        raise EvidenceValidationError(f"{label} is not a safe diagnostic value")
    if _contains_absolute_path(text):
        raise EvidenceValidationError(f"{label} contains a private absolute path")
    return text


def _require_sha256(value: object, label: str) -> str:
    text = _require_string(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise EvidenceValidationError(f"{label} must be lowercase SHA-256 hex")
    return text


def _portable_path(value: object, label: str) -> str:
    text = _require_string(value, label)
    if "\\" in text or ":" in text or "\x00" in text:
        raise EvidenceValidationError(f"{label} must be a portable relative path")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise EvidenceValidationError(f"{label} must be a normalized relative path")
    if path.as_posix() != text:
        raise EvidenceValidationError(f"{label} must use normalized POSIX separators")
    return text


def _contains_absolute_path(text: str) -> bool:
    return any(
        pattern.search(text) is not None
        for pattern in (
            _WINDOWS_ABSOLUTE_PATH_RE,
            _UNC_PATH_RE,
            _POSIX_PRIVATE_PATH_RE,
        )
    )


def _replace_case_insensitive(text: str, needle: str, replacement: str) -> str:
    if not needle:
        return text
    return re.sub(re.escape(needle), replacement, text, flags=re.IGNORECASE)


def redact_text(text: str, private_values: Mapping[str, str]) -> str:
    """Redact registered secrets/roots and residual absolute private paths."""

    _require_string(text, "text", allow_empty=True)
    registered: list[tuple[str, str]] = []
    for label, value in private_values.items():
        safe_label = _require_safe_value(label, "private value label")
        private = _require_string(value, f"private value {safe_label}")
        variants = {
            private,
            private.replace("\\", "/"),
            private.replace("/", "\\"),
            private.replace("\\", "\\\\"),
        }
        for variant in variants:
            if variant:
                registered.append((variant, f"<redacted:{safe_label}>"))
    result = text
    for value, replacement in sorted(registered, key=lambda item: len(item[0]), reverse=True):
        result = _replace_case_insensitive(result, value, replacement)
    result = _WINDOWS_ABSOLUTE_PATH_RE.sub("<redacted:path>", result)
    result = _UNC_PATH_RE.sub("<redacted:path>", result)
    result = _POSIX_PRIVATE_PATH_RE.sub("<redacted:path>", result)
    for value, _ in registered:
        if value.casefold() in result.casefold():
            raise EvidenceValidationError("registered private value remained after redaction")
    if _contains_absolute_path(result):
        raise EvidenceValidationError("absolute private path remained after redaction")
    return result


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def environment_profile_digest(profile: str) -> str:
    """Return the stable digest of a versioned effective-environment policy."""

    checked_profile = _require_safe_value(profile, "environment profile")
    definition = _ENVIRONMENT_PROFILE_DEFINITIONS.get(checked_profile)
    if definition is None:
        raise EvidenceValidationError("environment profile is not registered")
    return hashlib.sha256(_canonical_json_bytes(definition)).hexdigest()


def environment_profile_projection(
    profile: str,
    additions: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the canonical, non-secret projection approved for one command."""

    checked_profile = _require_safe_value(profile, "environment profile")
    definition = _ENVIRONMENT_PROFILE_DEFINITIONS.get(checked_profile)
    if definition is None:
        raise EvidenceValidationError("environment profile is not registered")
    projection = dict(definition["required_projection"])
    if additions is not None:
        for key, value in additions.items():
            checked_key = _require_string(key, "environment addition key")
            if (
                _ENVIRONMENT_NAME_RE.fullmatch(checked_key) is None
                or not checked_key.startswith("LOCALCAT_")
            ):
                raise EvidenceValidationError(
                    "environment additions must use the reserved LOCALCAT_ prefix"
                )
            projection[checked_key] = _require_string(
                value, f"environment addition {checked_key!r}", allow_empty=True
            )
    return {key: projection[key] for key in sorted(projection)}


def _windows_root_path() -> Path:
    if os.name != "nt":
        raise EvidenceValidationError("Windows root is unavailable on this host")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_windows_directory = kernel32.GetWindowsDirectoryW
    get_windows_directory.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    get_windows_directory.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = int(get_windows_directory(buffer, len(buffer)))
    if length == 0 or length >= len(buffer):
        raise EvidenceValidationError("Win32 could not identify the Windows directory")
    try:
        return Path(buffer.value).resolve(strict=True)
    except OSError as error:
        raise EvidenceValidationError("Windows directory is unavailable") from error


def _system_powershell_executable() -> Path:
    candidate = (
        _windows_root_path()
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        if not candidate.is_file():
            raise EvidenceValidationError("system Windows PowerShell is unavailable")
        return candidate.resolve(strict=True)
    except OSError as error:
        raise EvidenceValidationError("system Windows PowerShell is unavailable") from error


def _suspended_process_image_digest(
    process: subprocess.Popen[bytes],
    expected_executable: Path,
) -> str:
    """Bind the suspended process image to the selected system PowerShell bytes."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    query = kernel32.QueryFullProcessImageNameW
    query.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.PDWORD]
    query.restype = wintypes.BOOL
    buffer = ctypes.create_unicode_buffer(32768)
    length = wintypes.DWORD(len(buffer))
    if not query(
        wintypes.HANDLE(int(process._handle)),
        0,
        buffer,
        ctypes.byref(length),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        actual = Path(buffer.value[: length.value]).resolve(strict=True)
        expected = expected_executable.resolve(strict=True)
        if not os.path.samefile(actual, expected):
            raise EvidenceValidationError(
                "launched process image is not the selected system Windows PowerShell"
            )
        return _sha256_file(actual)
    except OSError as error:
        raise EvidenceValidationError(
            "unable to reprove the launched PowerShell process image"
        ) from error


def _parse_windows_powershell_probe(value: str) -> tuple[str, str, str]:
    """Validate V2 shell identity while retaining its full servicing version."""

    parts = value.strip().split("|")
    if len(parts) != 4:
        raise EvidenceValidationError("PowerShell identity probe has invalid shape")
    edition, version, is_64_bit, process_architecture = parts
    if edition != _WINDOWS_POWERSHELL_V2_EDITION:
        raise EvidenceValidationError("system shell is not Windows PowerShell Desktop")
    version_parts = version.split(".")
    if len(version_parts) != 4 or any(not item.isdigit() for item in version_parts):
        raise EvidenceValidationError("PowerShell version has invalid shape")
    if version_parts[:2] != ["5", "1"]:
        raise EvidenceValidationError("system shell is not Windows PowerShell 5.1")
    if is_64_bit != "True":
        raise EvidenceValidationError("system Windows PowerShell is not 64-bit")
    if process_architecture != _WINDOWS_POWERSHELL_V2_ARCHITECTURE:
        raise EvidenceValidationError("system Windows PowerShell process is not X64")
    return (
        _require_safe_value(edition, "PowerShell edition"),
        _require_safe_value(version, "PowerShell version"),
        _require_safe_value(process_architecture, "PowerShell process architecture"),
    )


def _powershell_identity_prelude(command_text: str) -> str:
    return (
        "$v=$PSVersionTable.PSVersion; $x=[Environment]::Is64BitProcess; "
        "$a=[System.Runtime.InteropServices.RuntimeInformation]::"
        "ProcessArchitecture.ToString(); "
        f"Write-Output ('{_SHELL_IDENTITY_MARKER}' + $PSVersionTable.PSEdition + "
        "'|' + $v.ToString() + '|' + $x.ToString() + '|' + $a); "
        f"& {{\n{command_text}\n}}"
    )


def _extract_powershell_identity(
    stdout_bytes: bytes,
) -> tuple[str, str, str, bytes]:
    lines = stdout_bytes.splitlines(keepends=True)
    if not lines:
        raise EvidenceValidationError("PowerShell command did not emit an identity prelude")
    try:
        first = lines[0].decode("utf-8", errors="strict").strip()
    except UnicodeError as error:
        raise EvidenceValidationError("PowerShell identity prelude is not UTF-8") from error
    if not first.startswith(_SHELL_IDENTITY_MARKER):
        raise EvidenceValidationError("PowerShell command identity prelude is missing")
    edition, version, architecture = _parse_windows_powershell_probe(
        first[len(_SHELL_IDENTITY_MARKER) :]
    )
    return edition, version, architecture, b"".join(lines[1:])


def _shell_version_matches_expectation(
    actual: object,
    expected: object,
    environment_profile: object,
) -> bool:
    if environment_profile != "CLEAN_WINDOWS_V2":
        return actual == expected
    if expected != _WINDOWS_POWERSHELL_V2_VERSION_POLICY or type(actual) is not str:
        return False
    parts = actual.split(".")
    return len(parts) == 4 and parts[:2] == ["5", "1"] and all(
        item.isdigit() for item in parts
    )


def _build_clean_windows_environment(
    profile: str,
    powershell_executable: str,
    additions: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, str]]:
    checked_profile = _require_safe_value(profile, "environment profile")
    definition = _ENVIRONMENT_PROFILE_DEFINITIONS.get(checked_profile)
    if definition is None:
        raise EvidenceValidationError("environment profile is not registered")
    if checked_profile not in {"CLEAN_WINDOWS_V1", "CLEAN_WINDOWS_V2"}:
        raise EvidenceValidationError("environment profile has no effective-environment builder")
    if os.name != "nt":
        raise EvidenceValidationError("clean Windows environment is unavailable on this host")
    windows_root = _windows_root_path()
    system32 = windows_root / "System32"
    system_drive = windows_root.drive
    if not system_drive:
        raise EvidenceValidationError("Windows system drive is unavailable")
    program_data = Path(f"{system_drive}\\ProgramData").resolve()
    powershell_directory = Path(powershell_executable).resolve().parent
    temp_root = Path(tempfile.gettempdir()).resolve()
    environment = {
        "ALLUSERSPROFILE": str(program_data),
        "ComSpec": str(system32 / "cmd.exe"),
        "PATH": os.pathsep.join(
            (str(powershell_directory), str(system32), str(windows_root))
        ),
        "PATHEXT": ".COM;.EXE;.BAT;.CMD",
        "ProgramData": str(program_data),
        "PYTHONNOUSERSITE": "1",
        "SystemDrive": system_drive,
        "SystemRoot": str(windows_root),
        "TEMP": str(temp_root),
        "TMP": str(temp_root),
        "WINDIR": str(windows_root),
    }
    projection = environment_profile_projection(checked_profile, additions)
    if additions is not None:
        for key, value in additions.items():
            checked_key = _require_string(key, "environment addition key")
            if (
                _ENVIRONMENT_NAME_RE.fullmatch(checked_key) is None
                or not checked_key.startswith("LOCALCAT_")
            ):
                raise EvidenceValidationError(
                    "environment additions must use the reserved LOCALCAT_ prefix"
                )
            environment[checked_key] = _require_string(
                value, f"environment addition {checked_key!r}", allow_empty=True
            )
    return (
        {key: environment[key] for key in sorted(environment)},
        {key: projection[key] for key in sorted(projection)},
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with candidate.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, path)
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise EvidenceValidationError(f"unable to hash {path.name}: {error}") from error
    return digest.hexdigest()


def _trusted_repository_file(repository_root: Path, key: object, label: str) -> Path:
    portable = _portable_path(key, label)
    candidate = repository_root.joinpath(*PurePosixPath(portable).parts)
    cursor = repository_root
    try:
        for part in PurePosixPath(portable).parts:
            cursor /= part
            is_junction = getattr(cursor, "is_junction", None)
            if cursor.is_symlink() or (callable(is_junction) and is_junction()):
                raise EvidenceValidationError(f"{label} traverses a reparse or symbolic link")
        if not candidate.is_file():
            raise EvidenceValidationError(f"{label} does not name a regular repository file")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repository_root)
    except EvidenceValidationError:
        raise
    except (OSError, ValueError) as error:
        raise EvidenceValidationError(f"{label} escapes the trusted repository root") from error
    return resolved


def _scenario_contract_key(value: object) -> str:
    portable = _portable_path(value, "scenario contract key")
    if _SCENARIO_CONTRACT_KEY_RE.fullmatch(portable) is None:
        raise EvidenceValidationError(
            "scenario contract key must be packaging/windows/evidence-scenarios/<lane>.json"
        )
    return portable


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _artifact_kind(path: str) -> str:
    parts = PurePosixPath(path).parts
    if parts and parts[0] == "logs":
        return "log"
    if parts and parts[0] == "diagnostics":
        return "diagnostic"
    if path == EVENT_LOG_NAME:
        return "event-log"
    if path.endswith((".sha256", "-sha256.json")):
        return "inventory"
    if path.endswith((".md", ".json")):
        return "report"
    return "other"


_ALLOWED_ARTIFACT_KINDS = frozenset(
    {"log", "diagnostic", "event-log", "inventory", "report"}
)


class EvidenceHarness:
    """Run commands and emit a portable, content-addressed evidence bundle."""

    def __init__(
        self,
        artifact_root: Path,
        *,
        repository_commit: str,
        repository_branch: str,
        run_id: str,
        environment: Mapping[str, str],
        repository_root: Path,
        scenario_contract_key: str,
        private_values: Mapping[str, str] | None = None,
        approved_ci_artifact_key: str | None = None,
    ) -> None:
        if not isinstance(artifact_root, Path):
            raise TypeError("artifact_root must be a Path")
        if not isinstance(repository_root, Path):
            raise TypeError("repository_root must be a Path")
        self.root = artifact_root.resolve()
        try:
            self.repository_root = repository_root.resolve(strict=True)
        except (OSError, EvidenceValidationError) as error:
            raise EvidenceValidationError("trusted repository root is unavailable") from error
        contract_key = _scenario_contract_key(scenario_contract_key)
        contract_path = _trusted_repository_file(
            self.repository_root,
            contract_key,
            "scenario contract key",
        )
        try:
            contract_path.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise EvidenceValidationError("scenario contract must be outside the artifact root")

        defaults = {
            "artifact-root": str(self.root),
            "working-directory": str(Path.cwd().resolve()),
            "user-home": str(Path.home().resolve()),
            "temp-root": str(Path(tempfile.gettempdir()).resolve()),
            "python-executable": str(Path(sys.executable).resolve()),
            "scenario-contract": str(contract_path),
            "repository-root": str(self.repository_root),
        }
        if private_values is not None:
            for label, value in private_values.items():
                if label in defaults:
                    raise EvidenceValidationError(f"duplicate private value label {label!r}")
                defaults[label] = value
        self.private_values = defaults

        commit = _require_string(repository_commit, "repository commit")
        if _COMMIT_RE.fullmatch(commit) is None:
            raise EvidenceValidationError("repository commit must be lowercase 40-hex")
        branch = _require_safe_value(repository_branch, "repository branch")
        if _RUN_ID_RE.fullmatch(run_id) is None:
            raise EvidenceValidationError("run id has invalid grammar")
        if frozenset(environment) != _ENVIRONMENT_KEYS:
            raise EvidenceValidationError("environment keys do not match the evidence schema")
        checked_environment: dict[str, str] = {}
        for key in sorted(_ENVIRONMENT_KEYS):
            value = _require_string(environment[key], f"environment.{key}")
            if redact_text(value, self.private_values) != value:
                raise EvidenceValidationError(
                    f"environment.{key} contains registered private data"
                )
            checked_environment[key] = value

        self.root.mkdir(parents=True, exist_ok=True)
        self.repository_commit = commit
        if redact_text(branch, self.private_values) != branch:
            raise EvidenceValidationError("repository branch contains registered private data")
        self.repository_branch = branch
        if redact_text(run_id, self.private_values) != run_id:
            raise EvidenceValidationError("run id contains registered private data")
        self.run_id = run_id
        artifact_key = (
            _portable_path(approved_ci_artifact_key, "approved CI artifact key")
            if approved_ci_artifact_key is not None
            else f"artifacts/windows/{commit}/{run_id}"
        )
        if artifact_key != f"artifacts/windows/{commit}/{run_id}" and not artifact_key.startswith(
            "ci/"
        ):
            raise EvidenceValidationError("approved CI artifact key must start with ci/")
        if redact_text(artifact_key, self.private_values) != artifact_key:
            raise EvidenceValidationError("artifact key contains registered private data")
        self.artifact_key = artifact_key
        self.environment = checked_environment
        if redact_text(contract_key, self.private_values) != contract_key:
            raise EvidenceValidationError("scenario contract key contains registered private data")
        contract = _load_json(contract_path)
        try:
            contract_payload = contract_path.read_bytes()
        except OSError as error:
            raise EvidenceValidationError("unable to read scenario contract bytes") from error
        if contract_payload != _canonical_json_bytes(contract):
            raise EvidenceValidationError("scenario contract is not canonical JSON")
        _validate_scenario_contract(contract)
        if contract["schema"] != SCENARIO_CONTRACT_SCHEMA_ID:
            raise EvidenceValidationError(
                "EvidenceHarness only produces current scenario contract schema v2; "
                "v1 is validation-only"
            )
        self.scenario_contract = contract
        self.scenario_contract_path = contract_path
        self.scenario_contract_key = contract_key
        self.scenario_contract_sha256 = _sha256_file(contract_path)

        powershell = _system_powershell_executable()
        self.powershell_executable = str(powershell)
        self.private_values["powershell-directory"] = str(
            Path(powershell).resolve().parent
        )
        self.powershell_flavor = "WINDOWS_POWERSHELL"
        self.powershell_selection_role = _WINDOWS_POWERSHELL_V2_SELECTION
        self.powershell_binary_sha256 = _sha256_file(powershell)
        version_environment, _ = _build_clean_windows_environment(
            _DEFAULT_ENVIRONMENT_PROFILE,
            self.powershell_executable,
            None,
        )
        try:
            version_probe = subprocess.run(
                [
                    powershell,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$v=$PSVersionTable.PSVersion; $x=[Environment]::Is64BitProcess; "
                    "$a=[System.Runtime.InteropServices.RuntimeInformation]::"
                    "ProcessArchitecture.ToString(); "
                    "Write-Output ($PSVersionTable.PSEdition + '|' + $v.ToString() + "
                    "'|' + $x.ToString() + '|' + $a)",
                ],
                env=version_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise EvidenceValidationError("unable to identify the PowerShell runtime") from error
        shell_identity = version_probe.stdout.decode("utf-8", errors="strict").strip()
        if version_probe.returncode != 0 or not shell_identity:
            raise EvidenceValidationError("unable to identify the PowerShell runtime")
        (
            self.powershell_edition,
            self.powershell_version,
            self.powershell_process_architecture,
        ) = _parse_windows_powershell_probe(shell_identity)
        self.command_records: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.release_outputs: dict[str, str] = {}
        self._command_ids: set[str] = set()
        self._operation_ids: set[str] = set()
        self._finalized = False

    def run_command(
        self,
        command_id: str,
        powershell_command: str,
        *,
        cwd: Path | None = None,
        timeout_seconds: float = 300.0,
        windowed: bool = False,
        environment_additions: Mapping[str, str] | None = None,
        environment_profile: str = _DEFAULT_ENVIRONMENT_PROFILE,
        private_values: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        if self._finalized:
            raise RuntimeError("evidence bundle is already finalized")
        if _COMMAND_ID_RE.fullmatch(command_id) is None:
            raise EvidenceValidationError("command id has invalid grammar")
        if command_id in self._command_ids:
            raise EvidenceValidationError(f"duplicate command id {command_id!r}")
        command_text = _require_string(powershell_command, "PowerShell command")
        if type(timeout_seconds) not in {int, float} or type(timeout_seconds) is bool:
            raise EvidenceValidationError("timeout_seconds must be numeric")
        if timeout_seconds <= 0:
            raise EvidenceValidationError("timeout_seconds must be positive")
        if type(windowed) is not bool:
            raise EvidenceValidationError("windowed must be an exact boolean")
        actual_cwd = Path.cwd().resolve()
        if cwd is not None:
            if not isinstance(cwd, Path):
                raise TypeError("cwd must be a Path")
            actual_cwd = cwd.resolve()
        try:
            actual_cwd.relative_to(self.repository_root)
        except ValueError:
            checked_cwd_role = "NON_REPOSITORY_CWD"
        else:
            checked_cwd_role = "REPOSITORY_TREE"
        checked_environment_profile = _require_safe_value(
            environment_profile, "environment profile"
        )
        environment_profile_sha256 = environment_profile_digest(
            checked_environment_profile
        )
        actual_env, environment_projection = _build_clean_windows_environment(
            checked_environment_profile,
            self.powershell_executable,
            environment_additions,
        )

        redactions = dict(self.private_values)
        redactions["command-cwd"] = str(actual_cwd)
        if private_values is not None:
            for label, value in private_values.items():
                if label in redactions:
                    raise EvidenceValidationError(f"duplicate private value label {label!r}")
                redactions[label] = value
        command_redacted = redact_text(command_text, redactions)
        if command_redacted != command_text:
            raise EvidenceValidationError(
                "PowerShell command must use portable placeholders instead of private values"
            )

        environment_projection = {
            key: redact_text(environment_projection[key], redactions)
            for key in sorted(environment_projection)
        }

        stdout_bytes = b""
        stderr_bytes = b""
        status = "FAIL"
        exit_code: int | None = None
        diagnostic_code: str | None = None
        command_shell_edition = "UNPROVEN"
        command_shell_version = "UNPROVEN"
        command_shell_architecture = "UNPROVEN"
        command_shell_binary_sha256: str | None = None
        launch_matches_initial_probe = False
        process: subprocess.Popen[bytes] | None = None
        job: _SuspendedWindowsJob | None = None
        launch_phase = "containment"
        try:
            job = _SuspendedWindowsJob()
            launch_phase = "process"
            process = subprocess.Popen(
                [
                    self.powershell_executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    _powershell_identity_prelude(command_text),
                ],
                cwd=actual_cwd,
                env=actual_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW,
            )
            launch_phase = "process-identity"
            command_shell_binary_sha256 = _suspended_process_image_digest(
                process,
                Path(self.powershell_executable),
            )
            launch_matches_initial_probe = (
                command_shell_binary_sha256 == self.powershell_binary_sha256
            )
            launch_phase = "assignment"
            job.assign_and_resume(process)
            launch_phase = "running"
            try:
                stdout_bytes, stderr_bytes = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                stdout_bytes, stderr_bytes = _stop_and_drain_process(process, job)
                status = "INTERRUPTED"
                diagnostic_code = "EVIDENCE.COMMAND.TIMEOUT"
            except KeyboardInterrupt:
                stdout_bytes, stderr_bytes = _stop_and_drain_process(process, job)
                status = "INTERRUPTED"
                diagnostic_code = "EVIDENCE.COMMAND.INTERRUPTED"
            else:
                exit_code = process.returncode
                status = "PASS" if exit_code == 0 else "FAIL"
        except OSError as error:
            if process is not None and process.poll() is None:
                stdout_bytes, process_stderr = _stop_and_drain_process(process, job)
                stderr_bytes = process_stderr + str(error).encode(
                    "utf-8", errors="replace"
                )
            else:
                stderr_bytes = str(error).encode("utf-8", errors="replace")
            diagnostic_code = (
                "EVIDENCE.COMMAND.LAUNCH_FAILED"
                if launch_phase == "process"
                else "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE"
            )
        finally:
            if job is not None:
                job.close()

        if launch_phase == "running":
            try:
                (
                    command_shell_edition,
                    command_shell_version,
                    command_shell_architecture,
                    stdout_bytes,
                ) = _extract_powershell_identity(stdout_bytes)
            except EvidenceValidationError:
                if (
                    status == "INTERRUPTED"
                    and diagnostic_code
                    in {
                        "EVIDENCE.COMMAND.TIMEOUT",
                        "EVIDENCE.COMMAND.INTERRUPTED",
                    }
                    and launch_matches_initial_probe
                ):
                    command_shell_edition = self.powershell_edition
                    command_shell_version = self.powershell_version
                    command_shell_architecture = self.powershell_process_architecture
                else:
                    status = "FAIL"
                    exit_code = None
                    diagnostic_code = "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE"

        stdout_text = redact_text(
            stdout_bytes.decode("utf-8", errors="replace"), redactions
        )
        stderr_text = redact_text(
            stderr_bytes.decode("utf-8", errors="replace"), redactions
        )
        stdout_path = self.root / "logs" / f"{command_id}.stdout.log"
        stderr_path = self.root / "logs" / f"{command_id}.stderr.log"
        _atomic_write_text(stdout_path, stdout_text)
        _atomic_write_text(stderr_path, stderr_text)

        if windowed and status != "PASS" and diagnostic_code is None:
            if not stdout_text and not stderr_text:
                diagnostic_code = "EVIDENCE.WINDOWED.SILENT_FAILURE"
            else:
                diagnostic_code = "EVIDENCE.WINDOWED.FAILURE"
        diagnostic_path: Path | None = None
        if diagnostic_code is not None:
            diagnostic_path = self.root / "diagnostics" / f"{command_id}.json"
            diagnostic = {
                "schema": "localcat.windows-release-diagnostic.v1",
                "command_id": command_id,
                "status": status,
                "stable_code": diagnostic_code,
                "exit_code": exit_code,
                "stdout_log": _relative_path(self.root, stdout_path),
                "stderr_log": _relative_path(self.root, stderr_path),
            }
            _atomic_write_bytes(diagnostic_path, _canonical_json_bytes(diagnostic))

        record: dict[str, object] = {
            "id": command_id,
            "shell": "powershell",
            "shell_flavor": self.powershell_flavor,
            "shell_version": command_shell_version,
            "shell_edition": command_shell_edition,
            "shell_process_architecture": command_shell_architecture,
            "shell_selection_role": self.powershell_selection_role,
            "shell_binary_sha256": command_shell_binary_sha256,
            "command": command_text,
            "command_sha256": hashlib.sha256(command_text.encode("utf-8")).hexdigest(),
            "cwd_role": checked_cwd_role,
            "environment_profile": checked_environment_profile,
            "environment_profile_sha256": environment_profile_sha256,
            "environment_projection": environment_projection,
            "windowed": windowed,
            "status": status,
            "exit_code": exit_code,
            "diagnostic_code": diagnostic_code,
            "stdout_log": _relative_path(self.root, stdout_path),
            "stdout_sha256": _sha256_file(stdout_path),
            "stderr_log": _relative_path(self.root, stderr_path),
            "stderr_sha256": _sha256_file(stderr_path),
            "diagnostic_marker": (
                _relative_path(self.root, diagnostic_path)
                if diagnostic_path is not None
                else None
            ),
            "diagnostic_sha256": (
                _sha256_file(diagnostic_path) if diagnostic_path is not None else None
            ),
        }
        self._command_ids.add(command_id)
        self.command_records.append(record)
        return dict(record)

    def record_event(
        self,
        *,
        operation_id: str,
        command_id: str | None = None,
        operation: str,
        profile: str | None,
        phase: str,
        result: str,
        stable_code: str | None = None,
        win32_code: int | None = None,
        filesystem: str | None = None,
        volume_class: str | None = None,
        identity_comparison: str = "NOT_OBSERVED",
        final_path_verdict: str = "NOT_OBSERVED",
        reparse_verdict: str = "NOT_OBSERVED",
        share_flags: Sequence[str] = (),
        lock_range: tuple[int, int] | None = None,
        recovery_result: str = "NOT_OBSERVED",
    ) -> dict[str, object]:
        if self._finalized:
            raise RuntimeError("evidence bundle is already finalized")
        if _RUN_ID_RE.fullmatch(operation_id) is None:
            raise EvidenceValidationError("operation id has invalid grammar")
        if operation_id in self._operation_ids:
            raise EvidenceValidationError(f"duplicate operation id {operation_id!r}")
        checked_command_id = None
        if command_id is not None:
            checked_command_id = _require_string(command_id, "event command id")
            if checked_command_id not in self._command_ids:
                raise EvidenceValidationError("event command id is not a recorded command")
        checked_operation = _require_safe_value(operation, "operation")
        checked_profile = (
            _require_safe_value(profile, "profile") if profile is not None else None
        )
        checked_phase = _require_safe_value(phase, "phase")
        checked_result = _require_enum(result, _EVENT_RESULTS, "result")
        checked_stable_code = None
        if stable_code is not None:
            checked_stable_code = _require_string(stable_code, "stable code")
            if _SAFE_CODE_RE.fullmatch(checked_stable_code) is None:
                raise EvidenceValidationError("stable code has invalid grammar")
        checked_win32_code = _require_optional_integer(win32_code, "win32 code")
        checked_filesystem = (
            _require_safe_value(filesystem, "filesystem")
            if filesystem is not None
            else None
        )
        checked_volume_class = (
            _require_safe_value(volume_class, "volume class")
            if volume_class is not None
            else None
        )
        checked_identity = _require_enum(
            identity_comparison, _IDENTITY_RESULTS, "identity comparison"
        )
        checked_final_path = _require_enum(
            final_path_verdict, _FINAL_PATH_RESULTS, "final path verdict"
        )
        checked_reparse = _require_enum(
            reparse_verdict, _REPARSE_RESULTS, "reparse verdict"
        )
        if isinstance(share_flags, (str, bytes)):
            raise EvidenceValidationError("share_flags must be a sequence")
        checked_share_flag_items: list[str] = []
        for flag in share_flags:
            checked_share_flag_items.append(_require_string(flag, "share flag"))
        checked_share_flags = tuple(sorted(set(checked_share_flag_items)))
        if any(flag not in _SHARE_FLAGS for flag in checked_share_flags):
            raise EvidenceValidationError("share_flags contains an unsupported value")
        if "NONE" in checked_share_flags and len(checked_share_flags) != 1:
            raise EvidenceValidationError("NONE cannot be combined with other share flags")
        checked_lock_range: dict[str, int] | None = None
        if lock_range is not None:
            if type(lock_range) is not tuple or len(lock_range) != 2:
                raise EvidenceValidationError("lock_range must be an exact two-item tuple")
            offset = _require_integer(lock_range[0], "lock offset", minimum=0)
            length = _require_integer(lock_range[1], "lock length", minimum=1)
            checked_lock_range = {"offset": offset, "length": length}
        checked_recovery = _require_enum(
            recovery_result, _RECOVERY_RESULTS, "recovery result"
        )
        if checked_win32_code is not None and not 0 <= checked_win32_code <= 0xFFFFFFFF:
            raise EvidenceValidationError("win32 code must fit an unsigned DWORD")
        if checked_result in {"FAIL", "INTERRUPTED", "RECOVERY_REQUIRED", "NOT_RUN"}:
            if checked_stable_code is None:
                raise EvidenceValidationError("non-success event requires a stable code")
        if checked_result == "PASS":
            if (
                checked_identity == "MISMATCH"
                or checked_final_path == "MISMATCH"
                or checked_reparse == "REJECTED"
                or checked_recovery == "RECOVERY_REQUIRED"
            ):
                raise EvidenceValidationError("PASS event contains a contradictory verdict")
        if checked_result == "RECOVERY_REQUIRED" and checked_recovery != "RECOVERY_REQUIRED":
            raise EvidenceValidationError("recovery-required event must bind its recovery result")
        private_event_values = {
            "operation id": operation_id,
            "command id": checked_command_id,
            "operation": checked_operation,
            "profile": checked_profile,
            "phase": checked_phase,
            "stable code": checked_stable_code,
            "filesystem": checked_filesystem,
            "volume class": checked_volume_class,
        }
        for label, text in private_event_values.items():
            if text is not None and redact_text(text, self.private_values) != text:
                raise EvidenceValidationError(f"{label} contains registered private data")
        event: dict[str, object] = {
            "schema": EVENT_SCHEMA_ID,
            "sequence": len(self.events) + 1,
            "operation_id": operation_id,
            "command_id": checked_command_id,
            "operation": checked_operation,
            "profile": checked_profile,
            "phase": checked_phase,
            "result": checked_result,
            "stable_code": checked_stable_code,
            "win32_code": checked_win32_code,
            "filesystem": checked_filesystem,
            "volume_class": checked_volume_class,
            "identity_comparison": checked_identity,
            "final_path_verdict": checked_final_path,
            "reparse_verdict": checked_reparse,
            "share_flags": list(checked_share_flags),
            "lock_range": checked_lock_range,
            "recovery_result": checked_recovery,
        }
        self.events.append(event)
        self._operation_ids.add(operation_id)
        return dict(event)

    def write_release_matrix(self, relative_path: str = "matrix.json") -> str:
        """Write the fact-derived matrix for the external scenario contract."""

        if self._finalized:
            raise RuntimeError("evidence bundle is already finalized")
        portable = _portable_path(relative_path, "matrix path")
        if not portable.endswith(".json"):
            raise EvidenceValidationError("matrix must use the .json release format")
        matrix, _ = _derive_release_matrix(
            self.scenario_contract,
            self.command_records,
            self.events,
        )
        _atomic_write_bytes(self.root / portable, _canonical_json_bytes(matrix))
        return portable

    def register_release_outputs(
        self,
        *,
        matrix: str,
        windows_fs_lock_checklist: str,
        frozen_source_packaging_checklist: str,
    ) -> None:
        """Bind the three mandatory release summaries into the manifest."""

        if self._finalized:
            raise RuntimeError("evidence bundle is already finalized")
        if self.release_outputs:
            raise RuntimeError("release outputs are already registered")
        candidates = {
            "matrix": matrix,
            "windows_fs_lock_checklist": windows_fs_lock_checklist,
            "frozen_source_packaging_checklist": frozen_source_packaging_checklist,
        }
        checked: dict[str, str] = {}
        for role, relative in candidates.items():
            portable = _portable_path(relative, f"{role} path")
            if redact_text(portable, self.private_values) != portable:
                raise EvidenceValidationError(f"{role} path contains registered private data")
            expected_suffix = ".json" if role == "matrix" else ".md"
            if not portable.endswith(expected_suffix):
                raise EvidenceValidationError(
                    f"{role} must use the {expected_suffix} release format"
                )
            path = _bundle_file(self.root, portable, f"{role} path")
            try:
                report_text = path.read_text(encoding="utf-8")
            except UnicodeError as error:
                raise EvidenceValidationError(f"{role} must be UTF-8 text") from error
            if redact_text(report_text, self.private_values) != report_text:
                raise EvidenceValidationError(f"{role} contains registered private data")
            checked[role] = portable
        if len(set(checked.values())) != len(checked):
            raise EvidenceValidationError("release output roles must name distinct files")
        self.release_outputs = checked

    def finalize(self) -> Path:
        if self._finalized:
            raise RuntimeError("evidence bundle is already finalized")
        if not self.command_records:
            raise EvidenceValidationError("release evidence requires at least one command")
        if frozenset(self.release_outputs) != _OUTPUT_KEYS:
            raise EvidenceValidationError(
                "release evidence requires matrix and both adaptation checklists"
            )
        _reject_reparse_entries(self.root)
        matrix_path = self.root / self.release_outputs["matrix"]
        matrix = _load_json(matrix_path)
        run_status = _validate_release_matrix(
            matrix,
            self.command_records,
            self.events,
            self.scenario_contract,
        )

        event_path = self.root / EVENT_LOG_NAME
        event_payload = b"".join(_canonical_json_bytes(event) for event in self.events)
        _atomic_write_bytes(event_path, event_payload)

        excluded = {MANIFEST_NAME, CHECKSUM_NAME}
        artifact_records: list[dict[str, object]] = []
        for path in sorted(item for item in self.root.rglob("*") if item.is_file()):
            relative = _relative_path(self.root, path)
            if relative in excluded:
                continue
            kind = _artifact_kind(relative)
            if kind not in _ALLOWED_ARTIFACT_KINDS:
                raise EvidenceValidationError(
                    f"unsupported evidence artifact {relative!r}"
                )
            try:
                artifact_text = path.read_text(encoding="utf-8")
            except UnicodeError as error:
                raise EvidenceValidationError(
                    f"evidence artifact {relative!r} must be UTF-8 text"
                ) from error
            if redact_text(artifact_text, self.private_values) != artifact_text:
                raise EvidenceValidationError(
                    f"evidence artifact {relative!r} contains registered private data"
                )
            artifact_records.append(
                {
                    "path": relative,
                    "kind": kind,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                }
            )
        manifest = {
            "schema": SCHEMA_ID,
            "repository": {
                "commit": self.repository_commit,
                "branch": self.repository_branch,
            },
            "run": {
                "id": self.run_id,
                "status": run_status,
                "artifact_key": self.artifact_key,
            },
            "environment": self.environment,
            "commands": self.command_records,
            "events": {
                "path": EVENT_LOG_NAME,
                "count": len(self.events),
                "sha256": _sha256_file(event_path),
            },
            "outputs": {
                role: {
                    "path": relative,
                    "sha256": _sha256_file(self.root / relative),
                }
                for role, relative in sorted(self.release_outputs.items())
            },
            "artifacts": artifact_records,
            "scenario_contract": {
                "key": self.scenario_contract_key,
                "sha256": self.scenario_contract_sha256,
            },
        }
        manifest_path = self.root / MANIFEST_NAME
        manifest_payload = _canonical_json_bytes(manifest)
        manifest_text = manifest_payload.decode("utf-8")
        if redact_text(manifest_text, self.private_values) != manifest_text:
            raise EvidenceValidationError("manifest metadata contains registered private data")
        _atomic_write_bytes(manifest_path, manifest_payload)

        checksum_records = [
            (record["path"], record["sha256"]) for record in artifact_records
        ]
        checksum_records.append((MANIFEST_NAME, _sha256_file(manifest_path)))
        checksum_text = "".join(
            f"{digest} *{path}\n" for path, digest in sorted(checksum_records)
        )
        _atomic_write_text(self.root / CHECKSUM_NAME, checksum_text)
        self._finalized = True
        return manifest_path


def _json_object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceValidationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_no_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError(f"unable to load {path.name}: {error}") from error


def _bundle_file(root: Path, relative: object, label: str) -> Path:
    portable = _portable_path(relative, label)
    candidate = root.joinpath(*PurePosixPath(portable).parts)
    cursor = root
    for part in PurePosixPath(portable).parts:
        cursor /= part
        is_junction = getattr(cursor, "is_junction", None)
        if cursor.is_symlink() or (callable(is_junction) and is_junction()):
            raise EvidenceValidationError(f"{label} traverses a reparse or symbolic link")
    if not candidate.is_file():
        raise EvidenceValidationError(f"{label} does not name a regular evidence file")
    try:
        candidate.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise EvidenceValidationError(f"{label} escapes the evidence root") from error
    return candidate


def _validate_environment_projection(
    profile: str,
    value: object,
    label: str,
) -> None:
    if type(value) is not dict or not value:
        raise EvidenceValidationError(f"{label} must be a non-empty object")
    if list(value) != sorted(value):
        raise EvidenceValidationError(f"{label} must be key-sorted")
    definition = _ENVIRONMENT_PROFILE_DEFINITIONS[profile]
    required_keys = set(definition["required_keys"])
    actual_keys = set(value)
    if not required_keys <= actual_keys:
        raise EvidenceValidationError(f"{label} is missing registered keys")
    if actual_keys & set(definition["forbidden_keys"]):
        raise EvidenceValidationError(f"{label} contains a forbidden key")
    allowed_prefix = str(definition["allowed_addition_prefix"])
    if any(
        key not in required_keys and not key.startswith(allowed_prefix)
        for key in actual_keys
    ):
        raise EvidenceValidationError(f"{label} contains an unregistered key")
    expected_projection = definition["required_projection"]
    for key in required_keys:
        if value[key] != expected_projection[key]:
            raise EvidenceValidationError(
                f"{label} does not match the registered profile"
            )
    for key, item in value.items():
        if _ENVIRONMENT_NAME_RE.fullmatch(key) is None:
            raise EvidenceValidationError(f"{label} key has invalid grammar")
        text = _require_string(item, f"{label} value {key!r}", allow_empty=True)
        if _contains_absolute_path(text):
            raise EvidenceValidationError(f"{label} contains a private path")


def _command_keys_for_schema(schema: str) -> frozenset[str]:
    if schema == SCHEMA_ID_V1:
        return _COMMAND_KEYS_V1
    if schema == SCHEMA_ID:
        return _COMMAND_KEYS
    raise EvidenceValidationError("manifest schema mismatch")


def _expectation_keys_for_schema(schema: str) -> frozenset[str]:
    if schema == SCENARIO_CONTRACT_SCHEMA_ID_V1:
        return _COMMAND_EXPECTATION_KEYS_V1
    if schema == SCENARIO_CONTRACT_SCHEMA_ID:
        return _COMMAND_EXPECTATION_KEYS
    raise EvidenceValidationError("scenario contract schema mismatch")


def _validate_command(
    root: Path,
    value: object,
    artifact_digests: Mapping[str, str],
    *,
    schema: str,
) -> str:
    if type(value) is not dict:
        raise EvidenceValidationError("command record must be an exact object")
    _require_exact_keys(value, _command_keys_for_schema(schema), "command record")
    command_id = _require_string(value["id"], "command id")
    if _COMMAND_ID_RE.fullmatch(command_id) is None:
        raise EvidenceValidationError("command id has invalid grammar")
    if value["shell"] != "powershell":
        raise EvidenceValidationError("command shell must be powershell")
    _require_enum(
        value["shell_flavor"],
        frozenset({"WINDOWS_POWERSHELL", "POWERSHELL_CORE"}),
        "command shell flavor",
    )
    _require_safe_value(value["shell_version"], "command shell version")
    if schema == SCHEMA_ID:
        _require_safe_value(value["shell_edition"], "command shell edition")
        _require_safe_value(
            value["shell_process_architecture"],
            "command shell process architecture",
        )
        _require_safe_value(value["shell_selection_role"], "command shell selection role")
        shell_binary_sha256 = value["shell_binary_sha256"]
        if shell_binary_sha256 is not None:
            _require_sha256(shell_binary_sha256, "command shell binary digest")
    command_text = _require_string(value["command"], "command text")
    if _contains_absolute_path(command_text):
        raise EvidenceValidationError("command text contains a private absolute path")
    command_sha256 = _require_sha256(value["command_sha256"], "command digest")
    if hashlib.sha256(command_text.encode("utf-8")).hexdigest() != command_sha256:
        raise EvidenceValidationError("command digest mismatch")
    _require_enum(
        value["cwd_role"],
        frozenset({"REPOSITORY_TREE", "NON_REPOSITORY_CWD"}),
        "command cwd role",
    )
    environment_profile = _require_safe_value(
        value["environment_profile"], "command environment profile"
    )
    environment_profile_sha256 = _require_sha256(
        value["environment_profile_sha256"], "environment profile digest"
    )
    if environment_profile_digest(environment_profile) != environment_profile_sha256:
        raise EvidenceValidationError("environment profile digest mismatch")
    _validate_environment_projection(
        environment_profile,
        value["environment_projection"],
        "environment projection",
    )
    identity_failure_codes = {
        "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
        "EVIDENCE.COMMAND.LAUNCH_FAILED",
        "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
    }
    identity_unproven = value["diagnostic_code"] in identity_failure_codes
    if environment_profile == "CLEAN_WINDOWS_V2":
        if schema != SCHEMA_ID:
            raise EvidenceValidationError("CLEAN_WINDOWS_V2 requires evidence schema v2")
        if not identity_unproven and (
            value["shell_flavor"] != "WINDOWS_POWERSHELL"
            or value["shell_edition"] != _WINDOWS_POWERSHELL_V2_EDITION
            or value["shell_process_architecture"]
            != _WINDOWS_POWERSHELL_V2_ARCHITECTURE
            or value["shell_selection_role"] != _WINDOWS_POWERSHELL_V2_SELECTION
            or not _shell_version_matches_expectation(
                value["shell_version"],
                _WINDOWS_POWERSHELL_V2_VERSION_POLICY,
                environment_profile,
            )
        ):
            raise EvidenceValidationError(
                "command shell does not satisfy CLEAN_WINDOWS_V2"
            )
        if not identity_unproven and shell_binary_sha256 is None:
            raise EvidenceValidationError(
                "CLEAN_WINDOWS_V2 command shell binary digest is missing"
            )
    windowed = _require_boolean(value["windowed"], "windowed")
    status = _require_enum(value["status"], _COMMAND_STATUSES, "command status")
    exit_code = _require_optional_integer(value["exit_code"], "command exit code")
    record_diagnostic_code = _require_optional_string(
        value["diagnostic_code"], "command diagnostic code"
    )
    if record_diagnostic_code is not None:
        _require_enum(record_diagnostic_code, _DIAGNOSTIC_CODES, "command diagnostic code")
    if status == "PASS" and exit_code != 0:
        raise EvidenceValidationError("PASS command must have exit code 0")
    if status == "FAIL" and exit_code == 0:
        raise EvidenceValidationError("FAIL command cannot have exit code 0")
    if status == "INTERRUPTED" and exit_code is not None:
        raise EvidenceValidationError("INTERRUPTED command must not claim an exit code")

    stream_texts: dict[str, str] = {}
    for stream in ("stdout", "stderr"):
        path_value = value[f"{stream}_log"]
        path = _bundle_file(root, path_value, f"{stream} log")
        portable = _portable_path(path_value, f"{stream} log")
        digest = _require_sha256(value[f"{stream}_sha256"], f"{stream} digest")
        if artifact_digests.get(portable) != digest or _sha256_file(path) != digest:
            raise EvidenceValidationError(f"{stream} log digest mismatch")
        text = path.read_text(encoding="utf-8")
        if _contains_absolute_path(text):
            raise EvidenceValidationError(f"{stream} log contains a private absolute path")
        stream_texts[stream] = text

    marker_value = value["diagnostic_marker"]
    marker_digest = value["diagnostic_sha256"]
    if marker_value is None or marker_digest is None:
        if marker_value is not None or marker_digest is not None:
            raise EvidenceValidationError("diagnostic marker and digest must be paired")
        if windowed and status != "PASS":
            raise EvidenceValidationError("failed windowed command requires a diagnostic marker")
        if status == "INTERRUPTED":
            raise EvidenceValidationError("interrupted command requires a diagnostic marker")
        if status == "FAIL" and exit_code is None:
            raise EvidenceValidationError("launch failure requires a diagnostic marker")
        if record_diagnostic_code is not None:
            raise EvidenceValidationError("diagnostic code requires a diagnostic marker")
    else:
        marker_path = _bundle_file(root, marker_value, "diagnostic marker")
        marker_portable = _portable_path(marker_value, "diagnostic marker")
        marker_sha256 = _require_sha256(marker_digest, "diagnostic digest")
        if (
            artifact_digests.get(marker_portable) != marker_sha256
            or _sha256_file(marker_path) != marker_sha256
        ):
            raise EvidenceValidationError("diagnostic marker digest mismatch")
        marker = _load_json(marker_path)
        if type(marker) is not dict:
            raise EvidenceValidationError("diagnostic marker must be an exact object")
        if marker_path.read_bytes() != _canonical_json_bytes(marker):
            raise EvidenceValidationError("diagnostic marker is not canonical JSON")
        _require_exact_keys(marker, _DIAGNOSTIC_KEYS, "diagnostic marker")
        if marker["schema"] != "localcat.windows-release-diagnostic.v1":
            raise EvidenceValidationError("diagnostic marker schema mismatch")
        if marker["command_id"] != command_id:
            raise EvidenceValidationError("diagnostic marker identity mismatch")
        if marker["status"] != status or marker["exit_code"] != exit_code:
            raise EvidenceValidationError("diagnostic marker result mismatch")
        if (
            marker["stdout_log"] != value["stdout_log"]
            or marker["stderr_log"] != value["stderr_log"]
        ):
            raise EvidenceValidationError("diagnostic marker log binding mismatch")
        diagnostic_code = _require_enum(
            marker["stable_code"], _DIAGNOSTIC_CODES, "diagnostic stable code"
        )
        if record_diagnostic_code != diagnostic_code:
            raise EvidenceValidationError("command diagnostic code does not match marker")
        if status == "PASS":
            raise EvidenceValidationError("PASS command cannot have a diagnostic marker")
        if diagnostic_code in {
            "EVIDENCE.COMMAND.INTERRUPTED",
            "EVIDENCE.COMMAND.TIMEOUT",
        }:
            if status != "INTERRUPTED" or exit_code is not None:
                raise EvidenceValidationError("interruption diagnostic result mismatch")
        elif diagnostic_code in {
            "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
            "EVIDENCE.COMMAND.LAUNCH_FAILED",
            "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
        }:
            if status != "FAIL" or exit_code is not None:
                raise EvidenceValidationError("launch diagnostic result mismatch")
        else:
            if not windowed or status != "FAIL" or exit_code in {None, 0}:
                raise EvidenceValidationError("windowed diagnostic result mismatch")
            silent = not stream_texts["stdout"] and not stream_texts["stderr"]
            if silent != (diagnostic_code == "EVIDENCE.WINDOWED.SILENT_FAILURE"):
                raise EvidenceValidationError("windowed diagnostic silence mismatch")
    return command_id


def _validate_event(
    value: object,
    *,
    expected_sequence: int | None = None,
    command_ids: set[str] | None = None,
) -> None:
    if type(value) is not dict:
        raise EvidenceValidationError("event must be an exact object")
    _require_exact_keys(value, _EVENT_KEYS, "event")
    if value["schema"] != EVENT_SCHEMA_ID:
        raise EvidenceValidationError("event schema mismatch")
    sequence = _require_integer(value["sequence"], "event sequence", minimum=1)
    if expected_sequence is not None and sequence != expected_sequence:
        raise EvidenceValidationError("event sequence is not contiguous")
    operation_id = _require_string(value["operation_id"], "operation id")
    if _RUN_ID_RE.fullmatch(operation_id) is None:
        raise EvidenceValidationError("operation id has invalid grammar")
    command_id = _require_optional_string(value["command_id"], "event command id")
    if command_id is not None:
        if _COMMAND_ID_RE.fullmatch(command_id) is None:
            raise EvidenceValidationError("event command id has invalid grammar")
        if command_ids is not None and command_id not in command_ids:
            raise EvidenceValidationError("event command id is not a recorded command")
    _require_safe_value(value["operation"], "operation")
    if value["profile"] is not None:
        _require_safe_value(value["profile"], "profile")
    _require_safe_value(value["phase"], "phase")
    result = _require_enum(value["result"], _EVENT_RESULTS, "event result")
    stable_code = _require_optional_string(value["stable_code"], "stable code")
    if stable_code is not None and _SAFE_CODE_RE.fullmatch(stable_code) is None:
        raise EvidenceValidationError("stable code has invalid grammar")
    win32_code = _require_optional_integer(value["win32_code"], "win32 code")
    if win32_code is not None and not 0 <= win32_code <= 0xFFFFFFFF:
        raise EvidenceValidationError("win32 code must fit an unsigned DWORD")
    for key in ("filesystem", "volume_class"):
        if value[key] is not None:
            _require_safe_value(value[key], key)
    identity = _require_enum(
        value["identity_comparison"], _IDENTITY_RESULTS, "identity comparison"
    )
    final_path = _require_enum(
        value["final_path_verdict"], _FINAL_PATH_RESULTS, "final path verdict"
    )
    reparse = _require_enum(
        value["reparse_verdict"], _REPARSE_RESULTS, "reparse verdict"
    )
    share_flags = value["share_flags"]
    if type(share_flags) is not list or any(type(flag) is not str for flag in share_flags):
        raise EvidenceValidationError("share_flags must be an exact string list")
    if share_flags != sorted(set(share_flags)):
        raise EvidenceValidationError("share_flags must be sorted and unique")
    if any(flag not in _SHARE_FLAGS for flag in share_flags):
        raise EvidenceValidationError("share_flags contains an unsupported value")
    if "NONE" in share_flags and len(share_flags) != 1:
        raise EvidenceValidationError("NONE cannot be combined with other share flags")
    lock_range = value["lock_range"]
    if lock_range is not None:
        if type(lock_range) is not dict:
            raise EvidenceValidationError("lock_range must be an exact object")
        _require_exact_keys(lock_range, frozenset({"offset", "length"}), "lock_range")
        _require_integer(lock_range["offset"], "lock offset", minimum=0)
        _require_integer(lock_range["length"], "lock length", minimum=1)
    recovery = _require_enum(
        value["recovery_result"], _RECOVERY_RESULTS, "recovery result"
    )
    if result in {"FAIL", "INTERRUPTED", "RECOVERY_REQUIRED", "NOT_RUN"}:
        if stable_code is None:
            raise EvidenceValidationError("non-success event requires a stable code")
    if result == "PASS" and (
        identity == "MISMATCH"
        or final_path == "MISMATCH"
        or reparse == "REJECTED"
        or recovery == "RECOVERY_REQUIRED"
    ):
        raise EvidenceValidationError("PASS event contains a contradictory verdict")
    if result == "RECOVERY_REQUIRED" and recovery != "RECOVERY_REQUIRED":
        raise EvidenceValidationError("recovery-required event must bind its recovery result")


def _validate_command_expectation(value: object, *, schema: str) -> str:
    if type(value) is not dict:
        raise EvidenceValidationError("command expectation must be an exact object")
    _require_exact_keys(
        value,
        _expectation_keys_for_schema(schema),
        "command expectation",
    )
    command_id = _require_string(value["id"], "expected command id")
    if _COMMAND_ID_RE.fullmatch(command_id) is None:
        raise EvidenceValidationError("expected command id has invalid grammar")
    _require_sha256(value["command_sha256"], "expected command digest")
    _require_enum(
        value["shell_flavor"],
        frozenset({"WINDOWS_POWERSHELL", "POWERSHELL_CORE"}),
        "expected shell flavor",
    )
    _require_safe_value(value["shell_version"], "expected shell version")
    if schema == SCENARIO_CONTRACT_SCHEMA_ID:
        _require_safe_value(value["shell_edition"], "expected shell edition")
        _require_safe_value(
            value["shell_process_architecture"],
            "expected shell process architecture",
        )
        _require_safe_value(
            value["shell_selection_role"],
            "expected shell selection role",
        )
    _require_enum(
        value["cwd_role"],
        frozenset({"REPOSITORY_TREE", "NON_REPOSITORY_CWD"}),
        "expected cwd role",
    )
    expected_environment_profile = _require_safe_value(
        value["environment_profile"], "expected environment profile"
    )
    if (
        _require_sha256(
            value["environment_profile_sha256"], "expected environment profile digest"
        )
        != environment_profile_digest(expected_environment_profile)
    ):
        raise EvidenceValidationError("expected environment profile digest mismatch")
    _validate_environment_projection(
        expected_environment_profile,
        value["environment_projection"],
        "expected environment projection",
    )
    if expected_environment_profile == "CLEAN_WINDOWS_V2":
        if schema != SCENARIO_CONTRACT_SCHEMA_ID:
            raise EvidenceValidationError(
                "CLEAN_WINDOWS_V2 requires scenario contract schema v2"
            )
        if (
            value["shell_flavor"] != "WINDOWS_POWERSHELL"
            or value["shell_version"] != _WINDOWS_POWERSHELL_V2_VERSION_POLICY
            or value["shell_edition"] != _WINDOWS_POWERSHELL_V2_EDITION
            or value["shell_process_architecture"]
            != _WINDOWS_POWERSHELL_V2_ARCHITECTURE
            or value["shell_selection_role"] != _WINDOWS_POWERSHELL_V2_SELECTION
        ):
            raise EvidenceValidationError(
                "CLEAN_WINDOWS_V2 requires system32 Windows PowerShell Desktop 5.1 X64"
            )
    _require_boolean(value["windowed"], "expected windowed")
    status = _require_enum(value["status"], _COMMAND_STATUSES, "expected command status")
    exit_code = _require_optional_integer(value["exit_code"], "expected exit code")
    diagnostic = _require_optional_string(value["diagnostic_code"], "expected diagnostic code")
    if diagnostic is not None:
        _require_enum(diagnostic, _DIAGNOSTIC_CODES, "expected diagnostic code")
    if status == "PASS" and (exit_code != 0 or diagnostic is not None):
        raise EvidenceValidationError("expected PASS command must be exit 0 without a diagnostic")
    if status == "FAIL" and exit_code == 0:
        raise EvidenceValidationError("expected FAIL command cannot use exit 0")
    if status == "INTERRUPTED" and exit_code is not None:
        raise EvidenceValidationError("expected INTERRUPTED command cannot claim an exit code")
    if diagnostic in {
        "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
        "EVIDENCE.COMMAND.LAUNCH_FAILED",
        "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
    }:
        raise EvidenceValidationError("scenario contract cannot approve a harness launch failure")
    return command_id


def _validate_scenario_contract(value: object) -> None:
    if type(value) is not dict:
        raise EvidenceValidationError("scenario contract must be an exact object")
    _require_exact_keys(value, _SCENARIO_CONTRACT_KEYS, "scenario contract")
    schema = value["schema"]
    if schema not in {SCENARIO_CONTRACT_SCHEMA_ID_V1, SCENARIO_CONTRACT_SCHEMA_ID}:
        raise EvidenceValidationError("scenario contract schema mismatch")
    _require_safe_value(value["id"], "scenario contract id")
    scenarios = value["scenarios"]
    if type(scenarios) is not list or not scenarios:
        raise EvidenceValidationError("scenario contract requires at least one scenario")

    scenario_ids: set[str] = set()
    command_ids: set[str] = set()
    event_ids: set[str] = set()
    mandatory = 0
    for scenario in scenarios:
        if type(scenario) is not dict:
            raise EvidenceValidationError("contract scenario must be an exact object")
        _require_exact_keys(scenario, _CONTRACT_SCENARIO_KEYS, "contract scenario")
        scenario_id = _require_string(scenario["id"], "contract scenario id")
        if _COMMAND_ID_RE.fullmatch(scenario_id) is None or scenario_id in scenario_ids:
            raise EvidenceValidationError("contract scenario ids must be valid and unique")
        scenario_ids.add(scenario_id)
        if _require_boolean(scenario["required"], "contract scenario required"):
            mandatory += 1
        _require_safe_value(
            scenario["expected_observation"], "contract expected observation"
        )
        commands = scenario["commands"]
        events = scenario["events"]
        if type(commands) is not list or type(events) is not list:
            raise EvidenceValidationError("contract commands/events must be exact lists")
        if not commands and not events:
            raise EvidenceValidationError("contract scenario requires a command or event")
        scenario_command_ids: set[str] = set()
        for expectation in commands:
            command_id = _validate_command_expectation(expectation, schema=str(schema))
            if command_id in command_ids:
                raise EvidenceValidationError("contract command ids must be globally unique")
            command_ids.add(command_id)
            scenario_command_ids.add(command_id)
        for expectation in events:
            if type(expectation) is not dict:
                raise EvidenceValidationError("event expectation must be an exact object")
            _require_exact_keys(expectation, _EVENT_EXPECTATION_KEYS, "event expectation")
            synthetic = {"schema": EVENT_SCHEMA_ID, "sequence": 1, **expectation}
            _validate_event(
                synthetic,
                expected_sequence=1,
                command_ids=scenario_command_ids,
            )
            operation_id = str(expectation["operation_id"])
            if operation_id in event_ids:
                raise EvidenceValidationError("contract event operation ids must be globally unique")
            event_ids.add(operation_id)
    if mandatory == 0:
        raise EvidenceValidationError("scenario contract requires a mandatory scenario")


def _derive_release_matrix(
    contract: Mapping[str, object],
    command_records: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], str]:
    expectation_keys = _expectation_keys_for_schema(str(contract["schema"]))
    commands_by_id = {str(record["id"]): record for record in command_records}
    if len(commands_by_id) != len(command_records):
        raise EvidenceValidationError("command ids must be unique")
    events_by_id = {str(event["operation_id"]): event for event in events}
    if len(events_by_id) != len(events):
        raise EvidenceValidationError("event operation ids must be unique")

    contracted_commands = {
        str(expectation["id"])
        for scenario in contract["scenarios"]
        for expectation in scenario["commands"]
    }
    contracted_events = {
        str(expectation["operation_id"])
        for scenario in contract["scenarios"]
        for expectation in scenario["events"]
    }
    if set(commands_by_id) - contracted_commands:
        raise EvidenceValidationError("scenario contract does not account for every command")
    if set(events_by_id) - contracted_events:
        raise EvidenceValidationError("scenario contract does not account for every event")

    projected: list[dict[str, object]] = []
    required_verdicts: list[str] = []
    any_harness_failure = any(
        record["diagnostic_code"]
        in {
            "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
            "EVIDENCE.COMMAND.LAUNCH_FAILED",
            "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
        }
        for record in command_records
    )
    any_interruption = any(
        record["status"] == "INTERRUPTED" for record in command_records
    ) or any(event["result"] == "INTERRUPTED" for event in events)
    for scenario in contract["scenarios"]:
        expected_commands = scenario["commands"]
        expected_events = scenario["events"]
        command_ids = sorted(
            str(item["id"]) for item in expected_commands if str(item["id"]) in commands_by_id
        )
        event_ids = sorted(
            str(item["operation_id"])
            for item in expected_events
            if str(item["operation_id"]) in events_by_id
        )
        complete = len(command_ids) == len(expected_commands) and len(event_ids) == len(
            expected_events
        )
        mismatch = False
        interrupted = False
        harness_failure = False
        if complete:
            for expected in expected_commands:
                actual = commands_by_id[str(expected["id"])]
                if actual["diagnostic_code"] in {
                    "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
                    "EVIDENCE.COMMAND.LAUNCH_FAILED",
                    "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
                }:
                    harness_failure = True
                if actual["status"] == "INTERRUPTED":
                    interrupted = True
                for key in expectation_keys:
                    if key == "shell_version" and _shell_version_matches_expectation(
                        actual[key],
                        expected[key],
                        expected["environment_profile"],
                    ):
                        continue
                    if actual[key] != expected[key]:
                        mismatch = True
            for expected in expected_events:
                actual = events_by_id[str(expected["operation_id"])]
                if actual["result"] == "INTERRUPTED":
                    interrupted = True
                for key in _EVENT_EXPECTATION_KEYS:
                    if actual[key] != expected[key]:
                        mismatch = True
        if not complete:
            verdict = "NOT_RUN"
            stable_code = "EVIDENCE.SCENARIO.NOT_RUN"
        elif interrupted:
            verdict = "INTERRUPTED"
            stable_code = "EVIDENCE.SCENARIO.INTERRUPTED"
        elif harness_failure:
            verdict = "FAIL"
            stable_code = "EVIDENCE.SCENARIO.HARNESS_FAILURE"
        elif mismatch:
            verdict = "FAIL"
            stable_code = "EVIDENCE.SCENARIO.OBSERVATION_MISMATCH"
        else:
            verdict = "PASS"
            stable_code = None
        any_harness_failure = any_harness_failure or harness_failure
        any_interruption = any_interruption or interrupted
        projected.append(
            {
                "id": scenario["id"],
                "required": scenario["required"],
                "expected_observation": scenario["expected_observation"],
                "command_ids": command_ids,
                "event_operation_ids": event_ids,
                "verdict": verdict,
                "stable_code": stable_code,
            }
        )
        if scenario["required"]:
            required_verdicts.append(verdict)

    if any_interruption or "INTERRUPTED" in required_verdicts:
        run_status = "INTERRUPTED"
    elif any_harness_failure or any(
        verdict in {"FAIL", "NOT_RUN"} for verdict in required_verdicts
    ):
        run_status = "FAIL"
    else:
        run_status = "PASS"
    return {"schema": "localcat.windows-release-matrix.v1", "scenarios": projected}, run_status


def _validate_release_matrix(
    value: object,
    command_records: Sequence[Mapping[str, object]],
    events: Sequence[Mapping[str, object]],
    scenario_contract: Mapping[str, object],
) -> str:
    if type(value) is not dict:
        raise EvidenceValidationError("release matrix must be an exact object")
    _require_exact_keys(value, _MATRIX_KEYS, "release matrix")
    expected, status = _derive_release_matrix(scenario_contract, command_records, events)
    if value != expected:
        raise EvidenceValidationError(
            "release matrix does not match externally supplied scenario facts"
        )
    return status


def _reject_reparse_entries(root: Path) -> None:
    for path in root.rglob("*"):
        if path.name.startswith("."):
            raise EvidenceValidationError("evidence bundle contains a hidden or temporary entry")
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or (callable(is_junction) and is_junction()):
            raise EvidenceValidationError("evidence bundle contains a reparse or symbolic link")


def validate_evidence_bundle(
    artifact_root: Path,
    *,
    repository_root: Path,
    scenario_contract_key: str,
    expected_repository_commit: str,
    expected_scenario_contract_sha256: str,
    expected_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Validate internal structure and, when supplied, an external manifest anchor."""

    if not isinstance(artifact_root, Path):
        raise TypeError("artifact_root must be a Path")
    if not isinstance(repository_root, Path):
        raise TypeError("repository_root must be a Path")
    checked_repository_commit = _require_string(
        expected_repository_commit, "expected repository commit"
    )
    if _COMMIT_RE.fullmatch(checked_repository_commit) is None:
        raise EvidenceValidationError("expected repository commit must be lowercase 40-hex")
    checked_contract_sha256 = _require_sha256(
        expected_scenario_contract_sha256,
        "expected scenario contract digest",
    )
    root = artifact_root.resolve()
    try:
        trusted_repository_root = repository_root.resolve(strict=True)
    except OSError as error:
        raise EvidenceValidationError("trusted repository root is unavailable") from error
    checked_contract_key = _scenario_contract_key(scenario_contract_key)
    external_contract_path = _trusted_repository_file(
        trusted_repository_root,
        checked_contract_key,
        "scenario contract key",
    )
    try:
        external_contract_path.relative_to(root)
    except ValueError:
        pass
    else:
        raise EvidenceValidationError("scenario contract must be outside the artifact root")
    scenario_contract = _load_json(external_contract_path)
    try:
        external_contract_payload = external_contract_path.read_bytes()
    except OSError as error:
        raise EvidenceValidationError("unable to read scenario contract bytes") from error
    if external_contract_payload != _canonical_json_bytes(scenario_contract):
        raise EvidenceValidationError("scenario contract is not canonical JSON")
    _validate_scenario_contract(scenario_contract)
    scenario_contract_sha256 = _sha256_file(external_contract_path)
    if scenario_contract_sha256 != checked_contract_sha256:
        raise EvidenceValidationError(
            "scenario contract does not match the external digest anchor"
        )
    _reject_reparse_entries(root)
    manifest_path = root / MANIFEST_NAME
    if expected_manifest_sha256 is not None:
        expected_digest = _require_sha256(
            expected_manifest_sha256, "expected manifest digest"
        )
        if _sha256_file(manifest_path) != expected_digest:
            raise EvidenceValidationError("manifest does not match the external digest anchor")
    manifest = _load_json(manifest_path)
    if type(manifest) is not dict:
        raise EvidenceValidationError("manifest must be an exact object")
    try:
        manifest_payload = manifest_path.read_bytes()
    except OSError as error:
        raise EvidenceValidationError("unable to read manifest bytes") from error
    if manifest_payload != _canonical_json_bytes(manifest):
        raise EvidenceValidationError("manifest is not canonical JSON")
    _require_exact_keys(manifest, _MANIFEST_KEYS, "manifest")
    manifest_schema = manifest["schema"]
    if manifest_schema not in {SCHEMA_ID_V1, SCHEMA_ID}:
        raise EvidenceValidationError("manifest schema mismatch")
    expected_contract_schema = (
        SCENARIO_CONTRACT_SCHEMA_ID_V1
        if manifest_schema == SCHEMA_ID_V1
        else SCENARIO_CONTRACT_SCHEMA_ID
    )
    if scenario_contract["schema"] != expected_contract_schema:
        raise EvidenceValidationError(
            "manifest and scenario contract schema generations do not match"
        )

    scenario_contract_reference = manifest["scenario_contract"]
    if type(scenario_contract_reference) is not dict:
        raise EvidenceValidationError("scenario contract reference must be an exact object")
    _require_exact_keys(
        scenario_contract_reference,
        _SCENARIO_CONTRACT_REFERENCE_KEYS,
        "scenario contract reference",
    )
    manifest_contract_key = _scenario_contract_key(scenario_contract_reference["key"])
    if manifest_contract_key != checked_contract_key:
        raise EvidenceValidationError(
            "manifest scenario contract key does not match the trusted repository key"
        )
    if (
        _require_sha256(
            scenario_contract_reference["sha256"], "scenario contract digest"
        )
        != scenario_contract_sha256
    ):
        raise EvidenceValidationError("manifest does not match the external scenario contract")

    repository = manifest["repository"]
    if type(repository) is not dict:
        raise EvidenceValidationError("repository must be an exact object")
    _require_exact_keys(repository, _REPOSITORY_KEYS, "repository")
    commit = _require_string(repository["commit"], "repository commit")
    if _COMMIT_RE.fullmatch(commit) is None:
        raise EvidenceValidationError("repository commit must be lowercase 40-hex")
    if commit != checked_repository_commit:
        raise EvidenceValidationError(
            "repository commit does not match the external commit anchor"
        )
    _require_safe_value(repository["branch"], "repository branch")

    run = manifest["run"]
    if type(run) is not dict:
        raise EvidenceValidationError("run must be an exact object")
    _require_exact_keys(run, _RUN_KEYS, "run")
    run_id = _require_string(run["id"], "run id")
    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise EvidenceValidationError("run id has invalid grammar")
    run_status = _require_enum(run["status"], _RUN_STATUSES, "run status")
    artifact_key = _portable_path(run["artifact_key"], "artifact key")
    default_artifact_key = f"artifacts/windows/{commit}/{run_id}"
    if artifact_key != default_artifact_key and not artifact_key.startswith("ci/"):
        raise EvidenceValidationError(
            "artifact key must use the repository layout or an approved ci/ key"
        )

    environment = manifest["environment"]
    if type(environment) is not dict or frozenset(environment) != _ENVIRONMENT_KEYS:
        raise EvidenceValidationError("environment does not match the evidence schema")
    for key, value in environment.items():
        text = _require_string(value, f"environment.{key}")
        if _contains_absolute_path(text):
            raise EvidenceValidationError(f"environment.{key} contains a private path")

    artifacts = manifest["artifacts"]
    if type(artifacts) is not list:
        raise EvidenceValidationError("artifacts must be an exact list")
    artifact_digests: dict[str, str] = {}
    declared_paths: list[str] = []
    for record in artifacts:
        if type(record) is not dict:
            raise EvidenceValidationError("artifact record must be an exact object")
        _require_exact_keys(record, _ARTIFACT_KEYS, "artifact record")
        relative = _portable_path(record["path"], "artifact path")
        if relative in artifact_digests:
            raise EvidenceValidationError(f"duplicate artifact path {relative!r}")
        path = _bundle_file(root, relative, "artifact path")
        kind = _require_string(record["kind"], "artifact kind")
        if kind != _artifact_kind(relative):
            raise EvidenceValidationError(f"artifact kind mismatch for {relative!r}")
        if kind not in _ALLOWED_ARTIFACT_KINDS:
            raise EvidenceValidationError(f"unsupported artifact kind for {relative!r}")
        size = _require_integer(record["bytes"], "artifact bytes", minimum=0)
        digest = _require_sha256(record["sha256"], "artifact digest")
        if path.stat().st_size != size or _sha256_file(path) != digest:
            raise EvidenceValidationError(f"artifact mismatch for {relative!r}")
        artifact_digests[relative] = digest
        declared_paths.append(relative)
        try:
            artifact_text = path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise EvidenceValidationError(
                f"evidence artifact {relative!r} must be UTF-8 text"
            ) from error
        if _contains_absolute_path(artifact_text):
            raise EvidenceValidationError(
                f"evidence artifact {relative!r} contains a private absolute path"
            )
    if declared_paths != sorted(declared_paths):
        raise EvidenceValidationError("artifact records must be path-sorted")

    actual_paths = sorted(
        _relative_path(root, path)
        for path in root.rglob("*")
        if path.is_file()
        and _relative_path(root, path) not in {MANIFEST_NAME, CHECKSUM_NAME}
    )
    if actual_paths != declared_paths:
        raise EvidenceValidationError("artifact inventory does not match bundle files")

    outputs = manifest["outputs"]
    if type(outputs) is not dict:
        raise EvidenceValidationError("release outputs must be an exact object")
    _require_exact_keys(outputs, _OUTPUT_KEYS, "release outputs")
    release_matrix: object | None = None
    for role, reference in outputs.items():
        if type(reference) is not dict:
            raise EvidenceValidationError(f"{role} reference must be an exact object")
        _require_exact_keys(reference, _OUTPUT_REFERENCE_KEYS, f"{role} reference")
        relative = _portable_path(reference["path"], f"{role} path")
        expected_suffix = ".json" if role == "matrix" else ".md"
        if not relative.endswith(expected_suffix):
            raise EvidenceValidationError(f"{role} has the wrong release format")
        path = _bundle_file(root, relative, f"{role} path")
        digest = _require_sha256(reference["sha256"], f"{role} digest")
        if artifact_digests.get(relative) != digest or _sha256_file(path) != digest:
            raise EvidenceValidationError(f"{role} digest mismatch")
        try:
            report_text = path.read_text(encoding="utf-8")
        except UnicodeError as error:
            raise EvidenceValidationError(f"{role} must be UTF-8 text") from error
        if _contains_absolute_path(report_text):
            raise EvidenceValidationError(f"{role} contains a private absolute path")
        if role == "matrix":
            release_matrix = _load_json(path)
            if path.read_bytes() != _canonical_json_bytes(release_matrix):
                raise EvidenceValidationError("release matrix is not canonical JSON")
    output_paths = [str(reference["path"]) for reference in outputs.values()]
    if len(output_paths) != len(set(output_paths)):
        raise EvidenceValidationError("release output roles must name distinct files")

    commands = manifest["commands"]
    if type(commands) is not list:
        raise EvidenceValidationError("commands must be an exact list")
    command_ids = [
        _validate_command(
            root,
            command,
            artifact_digests,
            schema=str(manifest_schema),
        )
        for command in commands
    ]
    if len(command_ids) != len(set(command_ids)):
        raise EvidenceValidationError("command ids must be unique")
    command_statuses = {
        str(command["id"]): str(command["status"]) for command in commands
    }

    events_summary = manifest["events"]
    if type(events_summary) is not dict:
        raise EvidenceValidationError("events summary must be an exact object")
    _require_exact_keys(events_summary, _EVENT_SUMMARY_KEYS, "events summary")
    if events_summary["path"] != EVENT_LOG_NAME:
        raise EvidenceValidationError("events summary must bind events.jsonl")
    event_path = _bundle_file(root, events_summary["path"], "events path")
    event_digest = _require_sha256(events_summary["sha256"], "events digest")
    if artifact_digests.get(EVENT_LOG_NAME) != event_digest or _sha256_file(event_path) != event_digest:
        raise EvidenceValidationError("event log digest mismatch")
    event_count = _require_integer(events_summary["count"], "event count", minimum=0)
    event_lines = event_path.read_text(encoding="utf-8").splitlines()
    if len(event_lines) != event_count:
        raise EvidenceValidationError("event count mismatch")
    events: list[Mapping[str, object]] = []
    for sequence, line in enumerate(event_lines, start=1):
        try:
            event = json.loads(line, object_pairs_hook=_json_object_no_duplicates)
        except json.JSONDecodeError as error:
            raise EvidenceValidationError(f"invalid event JSON: {error}") from error
        if (line + "\n").encode("utf-8") != _canonical_json_bytes(event):
            raise EvidenceValidationError("event log is not canonical JSONL")
        _validate_event(
            event,
            expected_sequence=sequence,
            command_ids=set(command_statuses),
        )
        events.append(event)

    if release_matrix is None:
        raise EvidenceValidationError("release matrix is missing")
    expected_status = _validate_release_matrix(
        release_matrix,
        commands,
        events,
        scenario_contract,
    )
    if run_status != expected_status:
        raise EvidenceValidationError("run status does not match mandatory scenario verdicts")

    checksum_path = root / CHECKSUM_NAME
    try:
        checksum_lines = checksum_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise EvidenceValidationError(f"unable to read checksums: {error}") from error
    expected_checksums = dict(artifact_digests)
    expected_checksums[MANIFEST_NAME] = _sha256_file(root / MANIFEST_NAME)
    actual_checksums: dict[str, str] = {}
    for line in checksum_lines:
        if " *" not in line:
            raise EvidenceValidationError("invalid checksum line")
        digest, relative = line.split(" *", 1)
        portable = _portable_path(relative, "checksum path")
        if portable in actual_checksums:
            raise EvidenceValidationError("duplicate checksum path")
        actual_checksums[portable] = _require_sha256(digest, "checksum digest")
    if actual_checksums != expected_checksums:
        raise EvidenceValidationError("checksum inventory does not match manifest")
    expected_checksum_lines = [
        f"{digest} *{relative}"
        for relative, digest in sorted(expected_checksums.items())
    ]
    if checksum_lines != expected_checksum_lines:
        raise EvidenceValidationError("checksum inventory is not canonical")
    return manifest


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate LocalCAT Windows evidence.")
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--scenario-contract-key", required=True)
    parser.add_argument("--expected-repository-commit", required=True)
    parser.add_argument("--expected-scenario-contract-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest = validate_evidence_bundle(
            args.artifact_root,
            repository_root=args.repository_root,
            scenario_contract_key=args.scenario_contract_key,
            expected_repository_commit=args.expected_repository_commit,
            expected_scenario_contract_sha256=(
                args.expected_scenario_contract_sha256
            ),
            expected_manifest_sha256=args.expected_manifest_sha256,
        )
    except EvidenceValidationError as error:
        print(f"WINDOWS_EVIDENCE_INVALID: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "schema": manifest["schema"],
                "run_id": manifest["run"]["id"],
                "status": manifest["run"]["status"],
                "commands": len(manifest["commands"]),
                "artifacts": len(manifest["artifacts"]),
                "validation_scope": (
                    "EXTERNALLY_ANCHORED"
                    if args.expected_manifest_sha256 is not None
                    else "INTERNAL_CONSISTENCY_ONLY"
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
