"""Encode pre-link facts and their native manifest; not a closure discoverer.

The build owner supplies exact collected bytes and its applied-source digest.
This codec never discovers an extra file, grants runtime authority, or includes
the resulting PE in its own pre-link input digest.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
import re
import struct


_ROLES = {"native": 1, "interpreter": 2, "bootstrap": 3, "critical-source": 4, "fixture": 5}


class ManifestInputError(ValueError):
    pass


@dataclass(frozen=True)
class SourceEntry:
    id: str
    path: str
    role: str
    content: bytes
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedManifest:
    prelink_bytes: bytes
    runtime_bytes: bytes

    def render_header(self, template: str) -> str:
        prelink = json.loads(self.prelink_bytes)
        root = hashlib.sha256(self.prelink_bytes).digest()
        substitutions = {
            "CANDIDATE_INPUT_DIGEST": prelink["candidate_input_digest"],
            "PRELINK_INPUT_DIGEST": root.hex(),
            "RUNTIME_MANIFEST_DIGEST": ",".join(map(str, hashlib.sha256(self.runtime_bytes).digest())),
            "RUNTIME_ROOT_DIGEST": ",".join(map(str, root)),
        }
        for name, value in substitutions.items():
            token = "@@" + name + "@@"
            if template.count(token) != 1:
                raise ManifestInputError(f"expected one header token: {name}")
            template = template.replace(token, value)
        if "@@" in template:
            raise ManifestInputError("unknown header token")
        return template


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ManifestInputError("expected lowercase SHA-256")
    return value


def _same_name(left: str, right: str) -> bool:
    if left.isascii() and right.isascii():
        return left.lower() == right.lower()
    if os.name != "nt":
        raise ManifestInputError("non-ASCII collision checks require Windows ordinal semantics")
    compare = ctypes.WinDLL("kernel32", use_last_error=True).CompareStringOrdinal
    compare.argtypes = [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_int]
    compare.restype = ctypes.c_int
    result = compare(left, -1, right, -1, 1)
    if result == 0:
        raise ManifestInputError("Windows filename comparison failed")
    return result == 2


def _validate_entry(entry: SourceEntry) -> None:
    if type(entry) is not SourceEntry:
        raise ManifestInputError("expected exact SourceEntry")
    if type(entry.id) is not str or re.fullmatch(r"[a-z0-9-]{1,63}", entry.id) is None:
        raise ManifestInputError("invalid entry id")
    if type(entry.role) is not str or entry.role not in _ROLES:
        raise ManifestInputError("unknown role")
    if type(entry.content) is not bytes or len(entry.content) > 32 * 1024 * 1024:
        raise ManifestInputError("invalid or oversized entry bytes")
    if entry.role in {"interpreter", "bootstrap", "critical-source"} and b"\0" in entry.content:
        raise ManifestInputError("embedded NUL in source")
    if type(entry.dependencies) is not tuple or any(type(name) is not str for name in entry.dependencies):
        raise ManifestInputError("dependencies must be an immutable id tuple")
    if len(entry.dependencies) != len(set(entry.dependencies)):
        raise ManifestInputError("duplicate dependency")
    path = entry.path
    if type(path) is not str or not path or "\\" in path:
        raise ManifestInputError("expected canonical forward-slash relative path")
    try:
        encoded = path.encode("utf-8", "strict")
        for part in path.split("/"):
            if not part or part in {".", ".."} or part.endswith((".", " ")):
                raise ManifestInputError("invalid path component")
            if len(part.encode("utf-16-le", "strict")) // 2 > 255:
                raise ManifestInputError("oversized path component")
            if any(ord(char) < 32 or char in '<>:"|?*' for char in part):
                raise ManifestInputError("unsafe Windows path character")
            base = part.split(".", 1)[0].rstrip(" ").upper()
            if base in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]", base):
                raise ManifestInputError("reserved Windows name")
            if part.lower() == "__pycache__":
                raise ManifestInputError("bytecode directory is forbidden")
    except UnicodeError as exc:
        raise ManifestInputError("invalid Unicode path") from exc
    if len(encoded) > 1024 or path.lower().endswith((".pyc", ".pyo", ".pyz")):
        raise ManifestInputError("oversized or bytecode path")


def prepare_manifest(entries, *, candidate_input_digest: str, applied_sources_digest: str) -> PreparedManifest:
    _digest(candidate_input_digest)
    _digest(applied_sources_digest)
    if type(entries) not in {list, tuple} or not 1 <= len(entries) <= 2048:
        raise ManifestInputError("entry count must be in 1..2048")
    for entry in entries:
        _validate_entry(entry)
    entries = sorted(entries, key=lambda entry: entry.id)
    ids = {entry.id: index for index, entry in enumerate(entries)}
    if len(ids) != len(entries):
        raise ManifestInputError("duplicate id")
    for index, entry in enumerate(entries):
        for other in entries[:index]:
            if _same_name(entry.path, other.path) or (
                (entry.role == "native" or other.role == "native")
                and _same_name(entry.path.split("/")[-1], other.path.split("/")[-1])
            ):
                raise ManifestInputError("duplicate Windows path or basename")
        if any(dependency not in ids for dependency in entry.dependencies):
            raise ManifestInputError("missing dependency")
    visiting, visited = set(), set()

    def visit(name):
        if name in visiting:
            raise ManifestInputError("dependency cycle")
        if name in visited:
            return
        visiting.add(name)
        for dependency in entries[ids[name]].dependencies:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in ids:
        visit(name)
    facts = [{"id": entry.id, "path": entry.path, "role": entry.role,
              "bytes": len(entry.content), "sha256": hashlib.sha256(entry.content).hexdigest(),
              "dependencies": sorted(entry.dependencies)} for entry in entries]
    prelink = json.dumps({"schema": "localcat.windows-frozen-prelink-manifest.v1",
                         "candidate_input_digest": candidate_input_digest,
                         "applied_sources_digest": applied_sources_digest, "entries": facts},
                        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    records, dependencies, strings = bytearray(), bytearray(), bytearray()
    for fact in facts:
        path, name = fact["path"].replace("/", "\\").encode("utf-8"), fact["id"].encode("ascii")
        path_offset = len(strings); strings.extend(path)
        id_offset = len(strings); strings.extend(name)
        dependency_offset = len(dependencies) // 4
        for dependency in fact["dependencies"]:
            dependencies.extend(struct.pack("<I", ids[dependency]))
        records.extend(struct.pack("<8IQ32s", path_offset, len(path), id_offset, len(name),
                                   _ROLES[fact["role"]], 0, dependency_offset, len(fact["dependencies"]),
                                   fact["bytes"], bytes.fromhex(fact["sha256"])))
    header = struct.pack("<8s10I32s", b"LCFMV001", 1, 80, len(facts), 72, 80,
                         80 + len(records), len(dependencies) // 4,
                         80 + len(records) + len(dependencies), len(strings), 0,
                         hashlib.sha256(prelink).digest())
    return PreparedManifest(prelink, header + records + dependencies + strings)
