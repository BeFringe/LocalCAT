"""Collect the complete Task 7.2 W3 producer input, not a product release spec.

The project collection is the unmodified 7.1 development closure. This module
adds content-addressed interpreter/wheel/CRT inputs and a native-only catalog.
Actual PE resolution, clean-build provenance and W3 admission remain build gates.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import stat
from typing import Any
import zipfile

from tools.generate_windows_frozen_manifest import _read, _require_fact, fact
from tools.windows_frozen_manifest import SourceEntry


_INITIALIZATION = {
    "encodings/__init__.py": "encoding-package",
    "encodings/aliases.py": "encoding-aliases",
    "encodings/utf_8.py": "encoding-utf8",
    "encodings/_win_cp_codecs.py": "encoding-win",
    "importlib/_bootstrap_external.py": "importlib-external",
}
_CRT = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll")
_QT_NATIVE = frozenset({
    "PySide6/pyside6.abi3.dll", "PySide6/Qt6Core.dll", "PySide6/Qt6Gui.dll", "PySide6/Qt6Widgets.dll",
    "PySide6/QtCore.pyd", "PySide6/QtGui.pyd", "PySide6/QtWidgets.pyd",
    "PySide6/plugins/platforms/qwindows.dll", "shiboken6/Shiboken.pyd", "shiboken6/shiboken6.abi3.dll",
})


@dataclass(frozen=True)
class ProducerInputs:
    entries: tuple[SourceEntry, ...]
    provenance: dict[str, Any]


def _identity(prefix: str, relative: str) -> str:
    return prefix + "-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]


def _module(relative: str) -> tuple[str, bool]:
    package = relative.endswith("/__init__.py")
    return relative[:-3].replace("/", ".").removesuffix(".__init__"), package


def _source_wheel_members(content: bytes, label: str, *, provenance: dict[str, Any] | None = None) -> dict[str, bytes]:
    """Prove the whole locked wheel, then exclude all bytecode from collection.

    Unlike the 7.1 source-bundle validator, this validates an upstream wheel
    whose non-collected members may include vendor bytecode. No archive is
    installed or put on the runtime path.
    """
    members: dict[str, bytes] = {}
    names: set[str] = set()
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        for info in archive.infolist():
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            if not name or "\\" in name or len(name.encode("utf-8")) > 1024:
                raise ValueError("unsafe wheel path: " + label)
            for part in name.split("/"):
                base = part.split(".", 1)[0].upper()
                if (not part or part in {".", ".."} or part.endswith((".", " "))
                        or len(part.encode("utf-16-le")) > 510
                        or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                        or base in {"CON", "PRN", "AUX", "NUL"}
                        or re.fullmatch(r"(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]", base)):
                    raise ValueError("unsafe wheel member: " + label + ":" + name)
            mode = info.external_attr >> 16
            if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR} or name.casefold() in names:
                raise ValueError("duplicate or special wheel member: " + label + ":" + name)
            names.add(name.casefold())
            if not info.is_dir():
                members[name] = archive.read(info)
    records = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise ValueError("ambiguous wheel RECORD: " + label)
    seen: set[str] = set()
    for row in csv.reader(io.StringIO(members[records[0]].decode("utf-8"))):
        if len(row) != 3 or row[0] in seen or row[0] not in members:
            raise ValueError("invalid wheel RECORD: " + label)
        name, digest, size = row
        seen.add(name)
        if name == records[0]:
            if digest or size:
                raise ValueError("wheel RECORD self-reference")
        else:
            expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(members[name]).digest()).decode().rstrip("=")
            if digest != expected or size != str(len(members[name])):
                raise ValueError("wheel RECORD mismatch: " + label + ":" + name)
    if seen != members.keys():
        raise ValueError("unlisted wheel member: " + label)
    excluded = {name: {**fact(payload), "reason": "bytecode-is-never-collected-or-executed"}
                for name, payload in members.items()
                if name.lower().endswith((".pyc", ".pyo", ".pyz"))
                or "__pycache__" in {part.casefold() for part in name.split("/")}}
    if provenance is not None:
        provenance[label] = {"wheel": fact(content), "record_path": records[0],
                             "record": fact(members[records[0]]), "verified_record_members": len(seen),
                             "excluded_members": excluded}
    return {name: payload for name, payload in members.items() if name not in excluded}


def collect_producer_inputs(
    repository: Path, project_manifest: dict[str, Any], runtime: Path,
    wheels: tuple[Path, ...], crt_root: Path, *, external_inputs: dict[str, Any] | None = None,
) -> ProducerInputs:
    import pefile

    if project_manifest.get("classification") != "DEVELOPMENT_CLOSURE_NOT_RELEASE":
        raise ValueError("Task 7.2 collector expects the separately identified 7.1 development collection")
    entries: dict[str, SourceEntry] = {}
    records: list[tuple[str, str, str | None, str | None, bool, str]] = []
    inputs: dict[str, Any] = {}
    wheel_records: dict[str, Any] = {}

    def add(entry: SourceEntry, logical: str | None, module: str | None, package: bool, kind: str, source: str) -> None:
        if entry.id in entries or any(item.path.casefold() == entry.path.casefold() for item in entries.values()):
            raise ValueError("duplicate producer input: " + source)
        entries[entry.id] = entry
        records.append((entry.id, entry.path, logical, module, package, kind))
        inputs[source] = fact(entry.content)

    for record in project_manifest["entries"]:
        relative = record["path"]
        content = _read(repository, relative)
        _require_fact(content, {name: record[name] for name in ("bytes", "sha256")}, relative)
        bootstrap = relative == "frozen_source_bootstrap.py"
        source = relative.endswith(".py")
        module, package = _module(relative) if source else (None, False)
        add(SourceEntry("bootstrap" if bootstrap else _identity("app", relative), "_internal/app/" + relative,
                        "bootstrap" if bootstrap else "critical-source" if source else "fixture", content),
            relative, None if bootstrap else module, package, "source" if source else "fixture", "repository:" + relative)
    if "bootstrap" not in entries:
        raise ValueError("7.1 project collection is missing the complete producer bootstrap")

    library = runtime / "Lib"
    for path in sorted(library.rglob("*.py")):
        relative = path.relative_to(library).as_posix()
        parts = relative.split("/")
        if any(part in {"test", "tests", "site-packages", "idlelib", "tkinter", "turtledemo", "ensurepip", "venv"} for part in parts[:-1]):
            continue
        module, package = _module(relative)
        entry_id = _INITIALIZATION.get(relative, _identity("stdlib", relative))
        add(SourceEntry(entry_id, "_internal/python/" + relative, "interpreter", _read(library, relative)),
            None, module, package, "source", "cpython:Lib/" + relative)

    native: dict[str, tuple[str, bytes, str | None, str]] = {}
    for basename in ("python314.dll", "python3.dll"):
        native[basename] = ("_internal/" + basename, _read(runtime, basename), None, "cpython:" + basename)
    for path in sorted((runtime / "DLLs").iterdir()):
        if path.suffix.lower() not in {".dll", ".pyd"} or path.name.startswith(("_test", "_tkinter")):
            continue
        basename = path.name.lower()
        module = path.name.split(".")[0] if path.suffix.lower() == ".pyd" else None
        if basename in native:
            raise ValueError("duplicate interpreter native basename")
        native[basename] = ("_internal/python/DLLs/" + path.name, _read(runtime, "DLLs/" + path.name), module, "cpython:DLLs/" + path.name)
    for basename in _CRT:
        native[basename] = ("_internal/" + basename, _read(crt_root, basename), None, "coherent-crt:" + basename)

    selected_native: set[str] = set()
    for wheel in sorted(wheels):
        content = wheel.read_bytes()
        inputs["wheel:" + wheel.name] = fact(content)
        members = _source_wheel_members(content, wheel.name, provenance=wheel_records)
        for relative, payload in sorted(members.items()):
            source = wheel.name + "!" + relative
            if relative in _QT_NATIVE:
                basename = Path(relative).name.lower()
                if basename in native:
                    raise ValueError("duplicate wheel native basename")
                module = relative[:-4].replace("/", ".") if relative.endswith(".pyd") else None
                native[basename] = ("_internal/packages/" + relative, payload, module, source)
                selected_native.add(relative)
            elif relative.endswith(".py") and relative.split("/")[0] in {"PySide6", "shiboken6", "openpyxl", "et_xmlfile"}:
                module, package = _module(relative)
                entry = SourceEntry(_identity("wheel", relative), "_internal/packages/" + relative, "interpreter", payload)
                existing = entries.get(entry.id)
                if existing is not None:
                    if existing != entry:
                        raise ValueError("conflicting shared wheel source member: " + source)
                    # The PySide6 distribution wheels repeat identical package
                    # sources. Retain one catalog identity and every provenance.
                    inputs[source] = fact(payload)
                else:
                    add(entry, None, module, package, "source", source)
    if selected_native != _QT_NATIVE:
        raise ValueError("complete declared Qt/Shiboken native roots are required")

    ids = {name: "python-runtime" if name == "python314.dll" else "vcruntime-runtime" if name == "vcruntime140.dll"
           else _identity("native", name) for name in native}
    native_facts: dict[str, Any] = {}
    for basename, (physical, content, module, source) in sorted(native.items()):
        imports: set[str] = set()
        forwarders: set[str] = set()
        with pefile.PE(data=content, fast_load=False, max_symbol_exports=100000) as pe:
            if getattr(pe.FILE_HEADER, "Machine", None) != 0x8664:
                raise ValueError("native input is not AMD64: " + source)
            for table in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
                imports.update(item.dll.decode("ascii").lower() for item in getattr(pe, table, ()))
            for symbol in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", ()):
                if symbol.forwarder:
                    library_name = symbol.forwarder.decode("ascii").rsplit(".", 1)[0].lower()
                    forwarders.add(library_name if library_name.endswith(".dll") else library_name + ".dll")
        dependencies = tuple(sorted(ids[name] for name in imports | forwarders if name in ids))
        add(SourceEntry(ids[basename], physical, "native", content, dependencies), None, module, False,
            "extension" if module else "fixture", source)
        native_facts[basename] = {"path": physical, **fact(content), "static_and_delay_imports": sorted(imports),
                                  "forwarder_libraries": sorted(forwarders),
                                  "declared_native_dependencies": list(dependencies),
                                  "system_resolution_pending": sorted((imports | forwarders) - ids.keys())}

    external = {name: value for name, value in inputs.items() if not name.startswith("repository:")}
    if external_inputs is not None and external != external_inputs:
        raise ValueError("producer interpreter/wheel/CRT inputs differ from the tracked external-input anchor")
    catalog = ("APPLICATION_ROOT = '_internal/app'\nENTRIES = " + repr(tuple(sorted(records))) + "\n").encode("utf-8")
    entries["catalog"] = SourceEntry("catalog", "_internal/producer_catalog.py", "interpreter", catalog)
    return ProducerInputs(tuple(entries[name] for name in sorted(entries)), {
        "classification": "TASK72_COMPLETE_PRODUCER_BUILD_INPUTS_NOT_PRODUCT_PRODUCTION_MANIFEST",
        "project_collection_classification": project_manifest["classification"],
        "project_collection_commit": project_manifest["repository_commit"],
        "inputs": inputs, "native": native_facts, "catalog": fact(catalog), "wheel_records": wheel_records,
        "entries": len(entries), "native_count": len(native),
    })


def collect_locked_producer_inputs(repository: Path, runtime: Path, wheel_root: Path, crt_root: Path) -> ProducerInputs:
    from tools.generate_windows_frozen_manifest import generate

    declaration_path = "packaging/windows/frozen-entry/producer-external-inputs.lock.json"
    declaration = json.loads(_read(repository, declaration_path))
    if (set(declaration) != {"schema", "classification", "inputs"}
            or declaration["schema"] != "localcat.windows-producer-external-inputs.v1"
            or declaration["classification"] != "TASK72_EXTERNAL_INPUTS_NOT_PRODUCT_RELEASE"):
        raise ValueError("invalid producer external-input declaration")
    collection = generate(repository, mode="development")
    if collection.manifest["dirty_project_inputs"]:
        raise ValueError("producer build requires clean tracked project inputs")
    result = collect_producer_inputs(repository, collection.manifest, runtime,
                                    tuple(sorted(wheel_root.glob("*.whl"))), crt_root,
                                    external_inputs=declaration["inputs"])
    return ProducerInputs(result.entries, {**result.provenance,
        "external_input_declaration": {"path": declaration_path, **fact(_read(repository, declaration_path))},
        "project_collection": collection.manifest,
        "project_collection_fact": fact(collection.manifest_bytes),
        "project_hook_fact": fact(collection.hook_bytes)})
