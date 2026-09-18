"""Deterministic post-build binding, not a runtime authority or W3 approval.

The build orchestrator supplies clean source facts and independently prepared
inputs. A later consumer MUST supply a trusted release digest from outside the
bundle; a self-consistent manifest is not its own trust anchor.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import re
import stat
import subprocess
import tarfile

from tools.windows_frozen_manifest import SourceEntry, prepare_manifest
from tools.windows_frozen_packaging import canonical, fact, tree_inventory, safe_relative


SCHEMA = "localcat.windows-frozen-release-binding.v1"
CLASSIFICATION = "BUILD_BINDING_NOT_W3_ACCEPTANCE"
SOURCE_SCOPES = ("tools", "packaging/windows/frozen-entry")


class ReleaseBindingError(ValueError):
    pass


def _digest(value, length=64):
    if type(value) is not str or re.fullmatch("[0-9a-f]{" + str(length) + "}", value) is None:
        raise ReleaseBindingError("invalid digest")
    return value


def _keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ReleaseBindingError("unexpected record fields")


def _fact(value):
    _keys(value, ("bytes", "sha256"))
    if type(value["bytes"]) is not int or value["bytes"] < 0:
        raise ReleaseBindingError("invalid byte count")
    _digest(value["sha256"])


def _inventory(value):
    if type(value) is not dict or not value:
        raise ReleaseBindingError("empty or invalid inventory")
    seen = set()
    for name, item in value.items():
        try:
            safe_relative(name)
        except ValueError as exc:
            raise ReleaseBindingError(str(exc)) from exc
        if name.casefold() in seen:
            raise ReleaseBindingError("duplicate inventory name")
        seen.add(name.casefold())
        _fact(item)


def _source_context(value):
    _keys(value, ("repository_commit", "source_inventory"))
    _digest(value["repository_commit"], 40)
    _inventory(value["source_inventory"])


def _no_reparse_ancestors(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if (part.is_symlink() or part.is_junction()
                or (part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400)):
            raise ReleaseBindingError("reparse build or distribution path")
    return path


def _actual_inventory(root):
    root = _no_reparse_ancestors(root)
    if not root.is_dir():
        raise ReleaseBindingError("missing distribution directory")
    try:
        for member in root.rglob("*"):
            info = member.lstat()
            if getattr(info, "st_file_attributes", 0) & 0x400 or not (
                    stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ReleaseBindingError("nonregular or reparse inventory member")
        return tree_inventory(root)
    except (OSError, ValueError) as exc:
        raise ReleaseBindingError(str(exc)) from exc


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ReleaseBindingError("duplicate JSON key")
            result[key] = value
        return result
    def invalid_number(_):
        raise ReleaseBindingError("invalid JSON number")
    try:
        return json.loads(data, object_pairs_hook=pairs,
                          parse_constant=invalid_number)
    except (UnicodeError, ValueError) as exc:
        raise ReleaseBindingError(str(exc)) from exc


def clean_source_context(repository):
    """Reject a dirty index/tree and bind exact HEAD bytes of build-owned roots.

    This is a synchronous clean-build check, not protection from a compromised
    compiler/OS. Raw archive comparison also catches assume-unchanged input drift
    and ignored extra inputs that a porcelain-only check would miss.
    """
    repository = _no_reparse_ancestors(repository)

    def git(*args):
        result = subprocess.run(["git", "-C", str(repository), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, check=False)
        if result.returncode:
            raise ReleaseBindingError("git source verification failed")
        return result.stdout

    if Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve() != repository.resolve():
        raise ReleaseBindingError("not the repository root")
    if git("status", "--porcelain=v1", "--untracked-files=all"):
        raise ReleaseBindingError("clean tracked source required")
    commit = _digest(git("rev-parse", "HEAD^{commit}").decode().strip(), 40)
    expected = {}
    archive = git("archive", "--format=tar", commit, "--", *SOURCE_SCOPES)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as members:
        for member in members:
            if member.isdir():
                continue
            if not member.isfile():
                raise ReleaseBindingError("nonregular tracked build input")
            expected[member.name] = fact(members.extractfile(member).read())
    actual = {}
    for scope in SOURCE_SCOPES:
        actual.update({scope + "/" + name: item for name, item in _actual_inventory(repository / scope).items()})
    if not expected or actual != expected:
        raise ReleaseBindingError("build input bytes differ from tracked HEAD")
    return {"repository_commit": commit, "source_inventory": actual}


def create_release_binding(dist, *, source_context, prepared, build_evidence):
    _source_context(source_context)
    _inventory(build_evidence)
    prelink = _json(prepared.prelink_bytes)
    _keys(prelink, ("schema", "candidate_input_digest", "applied_sources_digest", "entries"))
    if prelink["schema"] != "localcat.windows-frozen-prelink-manifest.v1":
        raise ReleaseBindingError("invalid prelink schema")
    if (build_evidence.get("prelink.json") != fact(prepared.prelink_bytes)
            or build_evidence.get("applied-sources.json", {}).get("sha256") != prelink["applied_sources_digest"]):
        raise ReleaseBindingError("build evidence does not bind prepared input")
    actual = _actual_inventory(dist)
    entries = []
    try:
        for item in prelink["entries"]:
            _keys(item, ("id", "path", "role", "bytes", "sha256", "dependencies"))
            # Validate the path before reading. Facts are then regenerated from
            # the actual bytes, not copied out of the prelink declaration.
            safe_relative(item["path"])
            if item["path"] not in actual:
                raise ReleaseBindingError("missing manifest member")
            entries.append(SourceEntry(item["id"], item["path"], item["role"],
                                       (Path(dist) / item["path"]).read_bytes(), tuple(item["dependencies"])))
        rebuilt = prepare_manifest(entries, candidate_input_digest=prelink["candidate_input_digest"],
                                   applied_sources_digest=prelink["applied_sources_digest"])
    except (OSError, TypeError, KeyError, ValueError) as exc:
        raise ReleaseBindingError("invalid or changed prelink payload") from exc
    if rebuilt != prepared:
        raise ReleaseBindingError("payload differs from prepared prelink/runtime bytes")
    expected_names = {entry.path for entry in entries} | {"localcat-runtime.manifest", "localcat-spike.exe"}
    if set(actual) != expected_names or actual.get("localcat-runtime.manifest") != fact(prepared.runtime_bytes):
        raise ReleaseBindingError("distribution or runtime manifest mismatch")
    value = {"schema": SCHEMA, "classification": CLASSIFICATION,
             "source_context": source_context, "candidate_input_digest": prelink["candidate_input_digest"],
             "prelink": fact(prepared.prelink_bytes), "runtime_manifest": fact(prepared.runtime_bytes),
             "applied_sources_digest": prelink["applied_sources_digest"],
             "executable": actual["localcat-spike.exe"], "dist": actual, "build_evidence": build_evidence}
    return canonical(value)


def verify_release_binding(data, dist, *, expected_release_sha256,
                           expected_repository_commit, expected_candidate_input_digest):
    """An external caller must retain the three expected values independently."""
    if fact(data)["sha256"] != _digest(expected_release_sha256):
        raise ReleaseBindingError("external release anchor mismatch")
    value = _json(data)
    _keys(value, ("schema", "classification", "source_context", "candidate_input_digest", "prelink",
                  "runtime_manifest", "applied_sources_digest", "executable", "dist", "build_evidence"))
    if value["schema"] != SCHEMA or value["classification"] != CLASSIFICATION or canonical(value) != data:
        raise ReleaseBindingError("invalid release schema, classification or encoding")
    _source_context(value["source_context"])
    if (value["source_context"]["repository_commit"] != _digest(expected_repository_commit, 40)
            or value["candidate_input_digest"] != _digest(expected_candidate_input_digest)):
        raise ReleaseBindingError("external commit or candidate mismatch")
    _digest(value["applied_sources_digest"])
    for name in ("prelink", "runtime_manifest", "executable"):
        _fact(value[name])
    for name in ("dist", "build_evidence"):
        _inventory(value[name])
    if (value["build_evidence"].get("prelink.json") != value["prelink"]
            or value["build_evidence"].get("applied-sources.json", {}).get("sha256") != value["applied_sources_digest"]
            or value["dist"].get("localcat-runtime.manifest") != value["runtime_manifest"]
            or value["dist"].get("localcat-spike.exe") != value["executable"]):
        raise ReleaseBindingError("inconsistent release graph")
    if _actual_inventory(dist) != value["dist"]:
        raise ReleaseBindingError("distribution differs from externally anchored release")
    return value
