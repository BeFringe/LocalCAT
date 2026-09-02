"""Narrow platform I/O and RSS facts for the offline TM benchmark owners.

The portable benchmark evidence persists only byte counts and SHA-256 values.
Live file identities remain inside the rooted platform authority used for one
capture and are never serialized as cross-process authority.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
from pathlib import Path, PurePath
import sys
import tempfile
import uuid
from typing import Iterable, Iterator

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import (
    BoundDirectoryAuthority,
    BoundRegularFile,
    CandidateContentFacts,
    ExactEmptyChildDirectoryRetirement,
    EntrySnapshot,
    PlatformFileBackend,
    PublishMode,
    RootedDirectoryAuthority,
    RootedFileSystem,
)


def _windows_extended_path(path: Path) -> Path:
    if sys.platform != "win32":
        return path
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        return path
    if raw.startswith("\\\\"):
        return Path(f"\\\\?\\UNC\\{raw[2:]}")
    if len(raw) >= 3 and raw[0].isalpha() and raw[1:3] == ":\\":
        return Path(f"\\\\?\\{raw}")
    return path


@dataclass(frozen=True, slots=True)
class PortableFileFacts:
    byte_count: int
    sha256: str
    content: bytes | None = None

    def __post_init__(self) -> None:
        if type(self.byte_count) is not int or self.byte_count < 0:
            raise ValueError("byte_count must be a non-negative exact int")
        if (
            type(self.sha256) is not str
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise ValueError("sha256 must be lowercase SHA-256 hex")
        if self.content is not None:
            if type(self.content) is not bytes:
                raise TypeError("content must be exact bytes or None")
            if len(self.content) != self.byte_count:
                raise ValueError("content length must match byte_count")
            if hashlib.sha256(self.content).hexdigest() != self.sha256:
                raise ValueError("content digest must match sha256")


@dataclass(frozen=True, slots=True)
class RootedArtifactFileFacts:
    path: Path
    facts: PortableFileFacts

    def __post_init__(self) -> None:
        if type(self.path) is not type(Path()) or not self.path.is_absolute():
            raise ValueError("artifact path must be an absolute native path")
        if type(self.facts) is not PortableFileFacts:
            raise TypeError("artifact facts must be exact PortableFileFacts")


def rooted_file_facts(path: Path, *, read_content: bool = False) -> PortableFileFacts:
    """Freshly bind one direct rooted file and return portable content facts."""

    if type(path) is not type(Path()):
        raise TypeError("path must be a concrete pathlib.Path")
    if type(read_content) is not bool:
        raise TypeError("read_content must be exact bool")
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    backend = compose_platform_file_backend(path.parent)
    root = None
    try:
        root = backend.bind_root(path.parent)
        return _bound_file_facts(
            backend,
            root,
            PurePath(path.name),
            read_content=read_content,
        )
    finally:
        if root is not None:
            root.close()


def _bound_file_facts(
    backend: RootedFileSystem,
    root: RootedDirectoryAuthority,
    relative: PurePath,
    *,
    read_content: bool,
) -> PortableFileFacts:
    """Capture one file through an already-retained rooted namespace."""

    regular = None
    try:
        regular = backend.open_regular(root, relative)
        first = regular.content_facts()
        if (
            first.snapshot.identity.kind != "regular"
            or first.snapshot.identity.link_count != 1
            or not first.snapshot.reparse_free
        ):
            raise ValueError("rooted file proof is not a direct regular file")
        content = regular.read_all() if read_content else None
        terminal = regular.snapshot()
        if terminal != first.snapshot:
            raise ValueError("rooted file changed during proof")
        root.reprove()
        return PortableFileFacts(
            byte_count=first.snapshot.byte_count,
            sha256=first.content_sha256.hex(),
            content=content,
        )
    finally:
        if regular is not None:
            regular.close()


def rooted_first_nonempty_line(path: Path) -> bytes:
    """Freshly read the first non-empty line without materializing the file."""

    if type(path) is not type(Path()):
        raise TypeError("path must be a concrete pathlib.Path")
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    backend = compose_platform_file_backend(path.parent)
    root = regular = None
    try:
        root = backend.bind_root(path.parent)
        regular = backend.open_regular(root, PurePath(path.name))
        expected = regular.snapshot()
        if (
            expected.identity.kind != "regular"
            or expected.identity.link_count != 1
            or not expected.reparse_free
            or root.inspect_entry(path.name) != expected
        ):
            raise ValueError("rooted file proof is not a direct regular file")
        offset = 0
        buffered = bytearray()
        while offset < expected.byte_count and len(buffered) < 64 * 1024:
            maximum = min(4096, expected.byte_count - offset)
            chunk = regular.read_at(offset, maximum, expected)
            buffered.extend(chunk)
            offset += len(chunk)
            lines = buffered.splitlines()
            complete_count = (
                len(lines)
                if buffered.endswith((b"\n", b"\r"))
                else max(0, len(lines) - 1)
            )
            for line in lines[:complete_count]:
                if line.strip():
                    if regular.snapshot() != expected:
                        raise ValueError("rooted file changed during read")
                    root.reprove()
                    return bytes(line)
        for line in buffered.splitlines():
            if line.strip():
                if regular.snapshot() != expected:
                    raise ValueError("rooted file changed during read")
                root.reprove()
                return bytes(line)
        raise ValueError("rooted file has no bounded non-empty line")
    finally:
        if regular is not None:
            regular.close()
        if root is not None:
            root.close()


def iter_rooted_file_lines(path: Path) -> Iterator[bytes]:
    """Stream exact lines from one freshly bound regular file."""

    if type(path) is not type(Path()):
        raise TypeError("path must be a concrete pathlib.Path")
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    backend = compose_platform_file_backend(path.parent)
    root = regular = None
    try:
        root = backend.bind_root(path.parent)
        regular = backend.open_regular(root, PurePath(path.name))
        expected = regular.snapshot()
        if (
            expected.identity.kind != "regular"
            or expected.identity.link_count != 1
            or not expected.reparse_free
            or root.inspect_entry(path.name) != expected
        ):
            raise ValueError("rooted file proof is not a direct regular file")
        offset = 0
        buffered = b""
        while offset < expected.byte_count:
            maximum = min(64 * 1024, expected.byte_count - offset)
            buffered += regular.read_at(offset, maximum, expected)
            offset += maximum
            while True:
                newline = buffered.find(b"\n")
                if newline < 0:
                    break
                yield buffered[: newline + 1]
                buffered = buffered[newline + 1 :]
        if buffered:
            yield buffered
        if regular.snapshot() != expected or root.inspect_entry(path.name) != expected:
            raise ValueError("rooted file changed during streamed read")
        root.reprove()
    finally:
        if regular is not None:
            regular.close()
        if root is not None:
            root.close()


def create_new_rooted_file(
    path: Path,
    chunks: Iterable[bytes],
) -> CandidateContentFacts:
    """Publish one exact CREATE_NEW file through the platform candidate port."""

    if type(path) is not type(Path()):
        raise TypeError("path must be a concrete pathlib.Path")
    if not path.is_absolute():
        raise ValueError("path must be absolute")
    backend = compose_platform_file_backend(path.parent)
    root = candidate = pending = None
    candidate_name = f".{path.name}.localcat-benchmark-{uuid.uuid4().hex}.candidate"
    try:
        root = backend.bind_root(path.parent)
        if root.inspect_entry(path.name) is not None:
            raise FileExistsError(path.name)
        candidate = root.create_candidate(candidate_name, private=False)
        expected = candidate.write_chunks(iter(chunks), maximum_bytes=(1 << 63) - 1)
        candidate.flush_content()
        pending = root.begin_publish(
            candidate,
            path.name,
            mode=PublishMode.CREATE_IF_ABSENT,
            lease=None,
        )
        candidate = None
        retained = pending.retained_destination()
        content = retained.content_facts()
        preliminary = pending.preliminary_facts()
        terminal = pending.terminal_reproof()
        if (
            content.snapshot.byte_count != expected.byte_count
            or content.content_sha256 != expected.content_sha256
            or preliminary.byte_count != expected.byte_count
            or preliminary.content_sha256 != expected.content_sha256
            or terminal != preliminary
        ):
            raise ValueError("published CREATE_NEW content facts drifted")
        root.reprove()
        return expected
    finally:
        if pending is not None:
            pending.close()
        if candidate is not None:
            candidate.close()
        if root is not None:
            root.close()


def create_windows_private_work_root(prefix: str) -> Path:
    """Create and freshly prove one Windows private benchmark directory."""

    if sys.platform != "win32":
        raise RuntimeError("Windows private work roots require win32")
    if type(prefix) is not str or not prefix:
        raise ValueError("prefix must be a non-empty exact str")
    base = Path(tempfile.gettempdir()).resolve(strict=True)
    backend = compose_platform_file_backend(base)
    root = parent = private = evidence = fresh_root = fresh = fresh_evidence = None
    name = f"{prefix}{uuid.uuid4().hex}"
    try:
        root = backend.bind_root(base)
        parent = backend.bind_parent(root, PurePath(name))
        if parent.inspect_entry(name) is not None:
            raise FileExistsError(name)
        private = backend.create_private_directory(parent, name)
        evidence = backend.prove_private(private)
        private.reprove()
        work_root = base / name
        if work_root.parent != base or any(_windows_extended_path(work_root).iterdir()):
            raise ValueError("private work root is not a fresh direct child")
        evidence.close()
        evidence = None
        private.close()
        private = None
        parent.close()
        parent = None
        root.close()
        root = None

        fresh_root = backend.bind_root(base)
        fresh = backend.bind_parent(
            fresh_root,
            PurePath(name, "owner-placeholder"),
        )
        fresh_evidence = backend.prove_private(fresh)
        fresh.reprove()
        return work_root
    finally:
        if fresh_evidence is not None:
            fresh_evidence.close()
        if fresh is not None:
            fresh.close()
        if fresh_root is not None:
            fresh_root.close()
        if evidence is not None:
            evidence.close()
        if private is not None:
            private.close()
        if parent is not None:
            parent.close()
        if root is not None:
            root.close()


def windows_rooted_directory_names(root_path: Path) -> tuple[str, ...]:
    """Observe names only while retaining and reproving the original root."""

    if sys.platform != "win32":
        raise RuntimeError("Windows rooted enumeration requires win32")
    if type(root_path) is not type(Path()) or not root_path.is_absolute():
        raise ValueError("root path must be an absolute native path")
    backend = compose_platform_file_backend(root_path)
    root = None
    try:
        root = backend.bind_root(root_path)
        root.reprove()
        names = tuple(
            sorted(entry.name for entry in _windows_extended_path(root_path).iterdir())
        )
        root.reprove()
        return names
    finally:
        if root is not None:
            root.close()


def windows_benchmark_artifact_family(
    *,
    run_root: Path,
    fixture_name: str,
    sidecar_name: str,
    manifest_name: str,
) -> tuple[RootedArtifactFileFacts, ...]:
    """Close one Windows activation namespace and capture its regular files."""

    if sys.platform != "win32":
        raise RuntimeError("Windows artifact enumeration requires win32")
    for name in (fixture_name, sidecar_name, manifest_name):
        if type(name) is not str or not name or "/" in name or "\\" in name:
            raise ValueError("artifact names must be safe direct components")
    direct_files = {
        fixture_name,
        sidecar_name,
        manifest_name,
        f".{sidecar_name}.localcat-activated-lineage.json",
        f".{sidecar_name}.localcat-initial-activation.lock",
    }
    if type(run_root) is not type(Path()) or not run_root.is_absolute():
        raise ValueError("run root must be an absolute native path")
    backend = compose_platform_file_backend(run_root)
    root_authority = None
    private_authority = private_evidence = quarantine_authority = None
    attempt_authority = None
    try:
        root_authority = backend.bind_root(run_root)
        root_authority.reprove()
        entries = tuple(_windows_extended_path(run_root).iterdir())
        root_authority.reprove()
        entry_names = {entry.name for entry in entries}
        private_names = tuple(
            sorted(
                name
                for name in entry_names
                if name.startswith(".localcat-activation-private-v1.")
            )
        )
        quarantine_name = ".localcat-activation-quarantine-v1"
        if len(private_names) != 1 or quarantine_name not in entry_names:
            raise ValueError("Windows activation directories are incomplete")
        observed_direct = entry_names - {private_names[0], quarantine_name}
        if observed_direct - direct_files:
            raise ValueError("run root contains foreign entries")
        missing_direct = sorted(direct_files - observed_direct)
        if missing_direct:
            raise ValueError(
                f"run root is missing required artifact {missing_direct[0]!r}"
            )

        private_authority = backend.bind_parent(
            root_authority,
            PurePath(private_names[0], "owner-placeholder"),
        )
        private_evidence = backend.prove_private(private_authority)
        private_authority.reprove()
        private_path = run_root / private_names[0]
        private_entries = tuple(_windows_extended_path(private_path).iterdir())
        private_authority.reprove()

        quarantine_authority = backend.bind_parent(
            root_authority,
            PurePath(quarantine_name, "owner-placeholder"),
        )
        quarantine_authority.reprove()
        quarantine_path = run_root / quarantine_name
        attempts = tuple(_windows_extended_path(quarantine_path).iterdir())
        quarantine_authority.reprove()
        if len(attempts) != 1 or not attempts[0].name.startswith("initial-"):
            raise ValueError("Windows activation quarantine is not closed")
        attempt_name = attempts[0].name
        attempt_authority = backend.bind_parent(
            root_authority,
            PurePath(quarantine_name, attempt_name, "owner-placeholder"),
        )
        attempt_authority.reprove()
        attempt_path = quarantine_path / attempt_name
        attempt_files = tuple(_windows_extended_path(attempt_path).iterdir())
        attempt_authority.reprove()

        expected_private_files = {
            "activation-journal-v3.json",
            "activation-publication-db-replaced-v1.json",
            "activation-publication-generation-published-v1.json",
            "activation-publication-manifest-published-v1.json",
            "device.key",
        }
        if {entry.name for entry in private_entries} != expected_private_files:
            raise ValueError("Windows private activation family is not closed")
        suffixes = sorted(
            ".manifest.tmp" if path.name.endswith(".manifest.tmp")
            else ".sqlite3.stage" if path.name.endswith(".sqlite3.stage")
            else "foreign"
            for path in attempt_files
        )
        if suffixes != [".manifest.tmp", ".sqlite3.stage"]:
            raise ValueError("Windows quarantine attempt family is not closed")

        relative_files = tuple(PurePath(name) for name in sorted(direct_files))
        relative_files += tuple(
            PurePath(private_names[0], name)
            for name in sorted(expected_private_files)
        )
        relative_files += tuple(
            PurePath(quarantine_name, attempt_name, path.name)
            for path in sorted(attempt_files, key=lambda path: path.name)
        )
        relative_files = tuple(
            sorted(relative_files, key=lambda relative: relative.as_posix())
        )
        artifacts = tuple(
            RootedArtifactFileFacts(
                path=run_root / relative,
                facts=_bound_file_facts(
                    backend,
                    root_authority,
                    relative,
                    read_content=False,
                ),
            )
            for relative in relative_files
        )
        root_authority.reprove()
        return artifacts
    finally:
        if attempt_authority is not None:
            attempt_authority.close()
        if quarantine_authority is not None:
            quarantine_authority.close()
        if private_evidence is not None:
            private_evidence.close()
        if private_authority is not None:
            private_authority.close()
        if root_authority is not None:
            root_authority.close()


def cleanup_windows_benchmark_artifact_family(
    *,
    backend: PlatformFileBackend,
    retirement: ExactEmptyChildDirectoryRetirement,
    run_root_authority: BoundDirectoryAuthority,
    run_root: Path,
    fixture_name: str,
    sidecar_name: str,
    manifest_name: str,
    _fault_injector: object | None = None,
) -> None:
    """Remove one already-closed Windows benchmark artifact family.

    The caller retains ``run_root_authority`` from run-root creation.  Every
    nested directory is rebound through that live authority before any file is
    removed.  The complete file family is captured once under retained live
    handles, then each name is unlinked only with that capture's identity;
    directories are consumed post-order by the exact-empty child capability.
    This is not a recursive/path deletion primitive.
    """

    if sys.platform != "win32":
        raise RuntimeError("Windows artifact cleanup requires win32")
    if not isinstance(backend, PlatformFileBackend):
        raise TypeError("backend must be a PlatformFileBackend")
    if not isinstance(retirement, ExactEmptyChildDirectoryRetirement):
        raise TypeError("retirement must be an exact-empty child capability")
    if not isinstance(run_root_authority, BoundDirectoryAuthority):
        raise TypeError("run_root_authority must be a bound directory authority")
    if _fault_injector is not None and not callable(_fault_injector):
        raise TypeError("_fault_injector must be callable or None")

    direct_files = {
        fixture_name,
        sidecar_name,
        manifest_name,
        f".{sidecar_name}.localcat-activated-lineage.json",
        f".{sidecar_name}.localcat-initial-activation.lock",
    }
    private_prefix = ".localcat-activation-private-v1."
    quarantine_name = ".localcat-activation-quarantine-v1"
    expected_private_files = {
        "activation-journal-v3.json",
        "activation-publication-db-replaced-v1.json",
        "activation-publication-generation-published-v1.json",
        "activation-publication-manifest-published-v1.json",
        "device.key",
    }
    authorities: dict[PurePath, BoundDirectoryAuthority] = {
        PurePath("."): run_root_authority,
    }
    # One-process cleanup receipts: FileObjectIdentity never leaves memory or
    # enters portable benchmark evidence.
    regulars: list[tuple[PurePath, BoundRegularFile, EntrySnapshot]] = []
    fresh_root: RootedDirectoryAuthority | None = None
    try:
        run_root_authority.reprove()
        root_names = {
            entry.name for entry in _windows_extended_path(run_root).iterdir()
        }
        run_root_authority.reprove()
        if not root_names:
            return
        private_names = tuple(
            sorted(name for name in root_names if name.startswith(private_prefix))
        )
        if len(private_names) != 1:
            raise ValueError("Windows activation private directory is not closed")
        private_name = private_names[0]
        if root_names != direct_files | {private_name, quarantine_name}:
            raise ValueError("Windows activation root family is not closed")
        private_relative = PurePath(private_name)
        quarantine_relative = PurePath(quarantine_name)
        authorities[private_relative] = retirement.bind_existing_child_directory(
            run_root_authority,
            private_name,
        )
        authorities[quarantine_relative] = retirement.bind_existing_child_directory(
            run_root_authority,
            quarantine_name,
        )
        private_path = run_root / private_name
        quarantine_path = run_root / quarantine_name
        authorities[private_relative].reprove()
        private_names_observed = {
            entry.name for entry in _windows_extended_path(private_path).iterdir()
        }
        authorities[private_relative].reprove()
        if private_names_observed != expected_private_files:
            raise ValueError("Windows private activation family is not closed")
        authorities[quarantine_relative].reprove()
        attempt_names = tuple(
            sorted(
                entry.name
                for entry in _windows_extended_path(quarantine_path).iterdir()
            )
        )
        authorities[quarantine_relative].reprove()
        if len(attempt_names) != 1 or not attempt_names[0].startswith("initial-"):
            raise ValueError("Windows quarantine family is not closed")
        attempt_relative = PurePath(quarantine_name, attempt_names[0])
        authorities[attempt_relative] = retirement.bind_existing_child_directory(
            authorities[quarantine_relative],
            attempt_names[0],
        )
        attempt_path = quarantine_path / attempt_names[0]
        authorities[attempt_relative].reprove()
        attempt_names_observed = tuple(
            sorted(
                entry.name
                for entry in _windows_extended_path(attempt_path).iterdir()
            )
        )
        authorities[attempt_relative].reprove()
        suffixes = sorted(
            ".manifest.tmp" if name.endswith(".manifest.tmp")
            else ".sqlite3.stage" if name.endswith(".sqlite3.stage")
            else "foreign"
            for name in attempt_names_observed
        )
        if suffixes != [".manifest.tmp", ".sqlite3.stage"]:
            raise ValueError("Windows quarantine attempt family is not closed")
        relatives = tuple(PurePath(name) for name in sorted(direct_files))
        relatives += tuple(
            PurePath(private_name, name) for name in sorted(expected_private_files)
        )
        relatives += tuple(
            PurePath(quarantine_name, attempt_names[0], name)
            for name in attempt_names_observed
        )
        relatives = tuple(sorted(relatives, key=lambda value: value.as_posix()))
        fresh_root = backend.bind_root(run_root)
        for relative in relatives:
            parent_authority = authorities[relative.parent]
            regular = backend.open_regular(fresh_root, relative)
            content = regular.content_facts()
            if (
                content.snapshot.identity.kind != "regular"
                or content.snapshot.identity.link_count != 1
                or not content.snapshot.reparse_free
                or parent_authority.inspect_entry(relative.name) != content.snapshot
            ):
                regular.close()
                raise ValueError("Windows benchmark artifact identity drifted")
            regulars.append((relative, regular, content.snapshot))
        for relative, _regular, snapshot in regulars:
            if authorities[relative.parent].inspect_entry(relative.name) != snapshot:
                raise ValueError("Windows benchmark artifact family drifted")
        for authority in authorities.values():
            authority.reprove()

        for relative, regular, snapshot in regulars:
            regular.close()
            if _fault_injector is not None:
                _fault_injector("after_capture_close", run_root / relative)
            parent_authority = authorities[relative.parent]
            parent_authority.unlink_owned(relative.name, snapshot.identity)
            if parent_authority.inspect_entry(relative.name) is not None:
                raise ValueError("Windows benchmark artifact unlink is not terminal")
        fresh_root.close()
        fresh_root = None

        for relative in (attempt_relative, private_relative, quarantine_relative):
            child = authorities.pop(relative)
            retirement.remove_empty_owned_directory(
                authorities[relative.parent],
                relative.name,
                child,
            )
        run_root_authority.reprove()
    finally:
        for _relative, regular, _snapshot in regulars:
            if not regular.closed:
                regular.close()
        if fresh_root is not None:
            fresh_root.close()
        for relative, authority in sorted(
            tuple(authorities.items()),
            key=lambda item: -len(item[0].parts),
        ):
            if relative != PurePath(".") and not authority.closed:
                authority.close()


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = (
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    )


def windows_peak_working_set_bytes() -> int:
    """Return GetProcessMemoryInfo.PeakWorkingSetSize for this process."""

    if sys.platform != "win32":
        raise RuntimeError("Windows RSS sampling requires win32")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = ctypes.c_void_p
    get_process_memory_info = psapi.GetProcessMemoryInfo
    get_process_memory_info.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ProcessMemoryCounters),
        ctypes.c_ulong,
    )
    get_process_memory_info.restype = ctypes.c_int
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not get_process_memory_info(
        get_current_process(),
        ctypes.byref(counters),
        ctypes.sizeof(counters),
    ):
        raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
    peak = int(counters.PeakWorkingSetSize)
    if peak < 1:
        raise ValueError("PeakWorkingSetSize is invalid")
    return peak


__all__ = [
    "PortableFileFacts",
    "RootedArtifactFileFacts",
    "cleanup_windows_benchmark_artifact_family",
    "create_new_rooted_file",
    "create_windows_private_work_root",
    "iter_rooted_file_lines",
    "rooted_first_nonempty_line",
    "rooted_file_facts",
    "windows_benchmark_artifact_family",
    "windows_peak_working_set_bytes",
    "windows_rooted_directory_names",
]
