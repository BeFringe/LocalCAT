"""Replay the approved ordered text patches into immutable build input bytes.

This is a strict subset of git unified diff: exact positions/context, regular
files only, no fuzz, rename, deletion, binary patch or permission changes. The
build owner must first collect and authenticate the pristine input against the
candidate lock. The expected base digest here binds that *entire* collected
inventory, including untouched sources. No filesystem, compiler, DLL or Python
startup is invoked by this codec; its output is not a realized-build approval.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType


class PatchInputError(ValueError):
    pass


@dataclass(frozen=True)
class Patch:
    id: str
    content: bytes


@dataclass(frozen=True)
class ReplayedSources:
    files: Mapping[str, bytes]
    provenance_bytes: bytes

    @property
    def applied_sources_digest(self) -> str:
        return hashlib.sha256(self.provenance_bytes).hexdigest()


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _path(path):
    if type(path) is not str or re.fullmatch(r"[A-Za-z0-9_./-]+", path) is None:
        raise PatchInputError("non-canonical build input path")
    for part in path.split("/"):
        if (not part or part in {".", "..", ".git"} or part.endswith(".")
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL"}
                or re.fullmatch(r"(?:COM|LPT)[1-9]", part.split(".")[0], re.I)):
            raise PatchInputError("unsafe build input path")
    return path


def _names(names):
    if type(names) not in {list, tuple}:
        raise PatchInputError("owner inventory must be an ordered list")
    checked = [_path(name) for name in names]
    if len({name.lower() for name in checked}) != len(checked):
        raise PatchInputError("duplicate or aliased owner path")
    return set(checked)


def _inventory(files):
    if not isinstance(files, Mapping) or not files:
        raise PatchInputError("empty or invalid source inventory")
    _names(list(files))
    if any(type(content) is not bytes for content in files.values()):
        raise PatchInputError("source inventory must contain exact bytes")
    return [{"path": name, "bytes": len(files[name]),
             "sha256": hashlib.sha256(files[name]).hexdigest()} for name in sorted(files)]


def source_inventory_digest(files: Mapping[str, bytes]) -> str:
    return hashlib.sha256(_canonical(_inventory(files))).hexdigest()


_DIFF = re.compile(rb"diff --git a/([^\n ]+) b/([^\n ]+)\n")
_INDEX = re.compile(rb"index [0-9a-f]+\.\.[0-9a-f]+(?: 100644)?\n")
_HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n")
_NO_NEWLINE = b"\\ No newline at end of file\n"


def _lf_lines(data):
    # Unified diff splits at LF only. A bare CR is source content, not syntax.
    parts = data.split(b"\n")
    return [part + b"\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _apply_section(lines, files, existing, new):
    match = _DIFF.fullmatch(lines[0])
    if not match or match[1] != match[2]:
        raise PatchInputError("invalid or renamed diff target")
    try:
        path = _path(match[1].decode("ascii"))
    except UnicodeError as exc:
        raise PatchInputError("non-ASCII diff target") from exc
    if path not in existing | new:
        raise PatchInputError("unowned diff target: " + path)
    index, created = 1, False
    if index < len(lines) and lines[index] == b"new file mode 100644\n":
        created, index = True, index + 1
    if index < len(lines) and _INDEX.fullmatch(lines[index]):
        index += 1
    old_header = b"--- /dev/null\n" if created else b"--- a/" + match[1] + b"\n"
    if lines[index:index + 2] != [old_header, b"+++ b/" + match[1] + b"\n"]:
        raise PatchInputError("unsupported patch metadata or mismatched headers")
    if created:
        if path not in new or path in files:
            raise PatchInputError("new source already exists or is not owned")
        original = []
    else:
        if path not in files:
            raise PatchInputError("patch source is missing")
        original = _lf_lines(files[path])
    index += 2
    cursor, output, hunks = 0, [], 0
    while index < len(lines):
        hunk = _HUNK.fullmatch(lines[index])
        if hunk is None:
            raise PatchInputError("expected a text hunk")
        old_start, old_count = int(hunk[1]), int(hunk[2] or b"1")
        new_start, new_count = int(hunk[3]), int(hunk[4] or b"1")
        old_offset = old_start - 1 if old_count else old_start
        new_offset = new_start - 1 if new_count else new_start
        if old_offset < cursor or old_offset > len(original):
            raise PatchInputError("overlapping or out-of-range hunk")
        output.extend(original[cursor:old_offset])
        if new_offset != len(output):
            raise PatchInputError("new hunk position mismatch")
        cursor, consumed, produced = old_offset, 0, 0
        operations = []
        index += 1
        while index < len(lines) and not lines[index].startswith(b"@@ "):
            line = lines[index]
            if not line or line[:1] not in (b" ", b"+", b"-"):
                raise PatchInputError("invalid text hunk line")
            operation, content = line[:1], line[1:]
            operations.append(operation)
            index += 1
            if index < len(lines) and lines[index] == _NO_NEWLINE:
                if not content.endswith(b"\n"):
                    raise PatchInputError("invalid no-newline marker")
                content = content[:-1]
                index += 1
            if operation in (b" ", b"-"):
                if cursor >= len(original) or original[cursor] != content:
                    raise PatchInputError("patch context conflict: " + path)
                cursor += 1
                consumed += 1
            if operation in (b" ", b"+"):
                output.append(content)
                produced += 1
        if (consumed, produced) != (old_count, new_count):
            raise PatchInputError("hunk line count mismatch")
        # Without boundary context, ordinary git apply anchors at BOF/EOF.
        # Do not silently accept the --unidiff-zero extension or fuzzy offsets.
        if (not operations or (operations[0] != b" " and old_offset != 0)
                or (operations[-1] != b" " and cursor != len(original))):
            raise PatchInputError("hunk lacks file-boundary context")
        hunks += 1
    if not hunks:
        raise PatchInputError("metadata-only patch")
    output.extend(original[cursor:])
    if any(not line.endswith(b"\n") for line in output[:-1]):
        raise PatchInputError("no-newline marker is not at end of file")
    result = b"".join(output)
    if not created and result == files[path]:
        raise PatchInputError("patch has no effect")
    files[path] = result
    return path


def replay_patch_series(pristine_files, patches, *, patch_contract, expected_base_digest):
    """Return candidate pre-link source bytes without mutating caller inputs.

    ``patch_contract`` is the approved candidate's patch section; extraction and
    candidate authentication belong to the build owner, not this text codec.
    Resulting PE bytes are intentionally not an input to this pre-link layer.
    """
    actual_digest = source_inventory_digest(pristine_files)
    if (type(expected_base_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_base_digest) is None
            or actual_digest != expected_base_digest):
        raise PatchInputError("pristine base digest mismatch")
    try:
        existing = _names(patch_contract["owned_existing_files"])
        new = _names(patch_contract["owned_new_files"])
        series = [item["id"] for item in patch_contract["series"]]
    except (KeyError, TypeError) as exc:
        raise PatchInputError("invalid patch contract") from exc
    _names(sorted(existing | new))
    if (not existing or not new or existing & new or not existing <= pristine_files.keys()
            or new & pristine_files.keys()):
        raise PatchInputError("owned source base inventory mismatch")
    if (not series or any(type(name) is not str or re.fullmatch(r"[a-z0-9-]+", name) is None
                          for name in series) or len(series) != len(set(series))):
        raise PatchInputError("invalid approved patch series")
    if (type(patches) not in {list, tuple} or any(type(patch) is not Patch for patch in patches)
            or [patch.id for patch in patches] != series):
        raise PatchInputError("patch series order mismatch")
    files, patch_facts = dict(pristine_files), []
    for patch in patches:
        if type(patch.content) is not bytes or not patch.content or b"\0" in patch.content:
            raise PatchInputError("invalid patch bytes")
        lines = _lf_lines(patch.content)
        starts = [index for index, line in enumerate(lines) if line.startswith(b"diff --git ")]
        if not starts or starts[0] != 0 or not patch.content.endswith(b"\n"):
            raise PatchInputError("expected exact git unified diff")
        starts.append(len(lines))
        touched = set()
        for start, end in zip(starts, starts[1:]):
            path = _apply_section(lines[start:end], files, existing, new)
            if path in touched:
                raise PatchInputError("duplicate file section")
            touched.add(path)
        patch_facts.append({"id": patch.id, "bytes": len(patch.content),
                            "sha256": hashlib.sha256(patch.content).hexdigest(),
                            "paths": sorted(touched)})
    if not new <= files.keys():
        raise PatchInputError("missing required new sources")
    provenance = _canonical({"schema": "localcat.windows-frozen-applied-sources.v1",
                             "base_digest": actual_digest, "base_files": _inventory(pristine_files),
                             "patches": patch_facts, "files": _inventory(files),
                             "result_digest": source_inventory_digest(files)})
    return ReplayedSources(MappingProxyType(files), provenance)
