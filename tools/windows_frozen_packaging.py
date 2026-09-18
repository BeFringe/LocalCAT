"""Source-only PyInstaller EXE/PKG/COLLECT packaging for the W3 spike.

This component records its build inputs and verifies the collected payload. It
does not approve those inputs or substitute for the final W3 evidence gate.
The child is run with -I -S and an explicit copied build-dependency directory;
the application never executes this build helper or the empty CArchive.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tarfile


APPLICATION_MANIFEST = b'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
  </requestedPrivileges></security></trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
  </windowsSettings></application>
</assembly>
'''

# These are build-time packages only. No ambient site-packages path is admitted
# to the isolated child, and no hook discovery or Analysis is performed.
DEPENDENCIES = (
    (("altgraph",), "altgraph-*.dist-info"),
    (("packaging",), "packaging-*.dist-info"),
    (("pefile.py", "peutils.py", "ordlookup"), "pefile-*.dist-info"),
    (("win32ctypes",), "pywin32_ctypes-*.dist-info"),
)
BUILD_SOURCE_FILES = (
    "tools/windows_frozen_packaging.py",
    "tools/probe_windows_frozen_custom_runw.py",
    "tools/windows_frozen_manifest.py",
    "tools/windows_frozen_patch_series.py",
    "tools/audit_w3_stock_bootloader.py",
    "tools/prepare_windows_frozen_entry_inputs.py",
)


class PackagingInputError(ValueError):
    pass


def fact(data):
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def safe_relative(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise PackagingInputError("unsafe package path")
    parts = name.split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts):
        raise PackagingInputError("unsafe package path")
    if any(part.lower() == "__pycache__" for part in parts) or name.lower().endswith((".pyc", ".pyo", ".pyz")):
        raise PackagingInputError("bytecode package path")
    return name


def render_spec(toc):
    seen = set()
    for name, source, kind in toc:
        safe_relative(name)
        safe_relative(source)
        if name.casefold() in seen or kind not in {"DATA", "BINARY"} or not source:
            raise PackagingInputError("duplicate or executable-source collection member")
        seen.add(name.casefold())
    # Upstream unconditionally injects Common-Controls, even for supplied XML.
    # Override before Target's guts check AND assemble, never edit the final PE.
    # This is pinned to the reviewed EXE/Target implementation in the sdist.
    return (
        "class RetainedSourceEXE(EXE):\n"
        "    def __postinit__(self):\n"
        f"        self.manifest = {APPLICATION_MANIFEST!r}\n"
        "        super().__postinit__()\n\n"
        "exe = RetainedSourceEXE([], name='localcat-spike', console=False,\n"
        "    exclude_binaries=True, append_pkg=True, contents_directory='.',\n"
        "    icon='NONE', strip=False, upx=False, uac_admin=False, uac_uiaccess=False)\n"
        f"toc = [(name, os.path.join(SPECPATH, source), kind) for name, source, kind in {list(toc)!r}]\n"
        "collection = COLLECT(exe, toc, name='localcat-spike', strip=False, upx=False)\n"
    )


def extract_packager(archive, expected, destination):
    data = Path(archive).read_bytes()
    if fact(data) != {key: expected[key] for key in ("bytes", "sha256")}:
        raise PackagingInputError("packager sdist digest mismatch")
    members = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for entry in tar.getmembers():
            parts = PurePosixPath(entry.name).parts
            if len(parts) < 2 or parts[1] != "PyInstaller":
                continue
            relative = safe_relative("/".join(parts[1:]))
            if entry.isdir():
                continue
            if not entry.isfile() or relative.casefold() in {name.casefold() for name in members}:
                raise PackagingInputError("linked or duplicate packager member")
            members[relative] = tar.extractfile(entry).read()
    if "PyInstaller/__init__.py" not in members:
        raise PackagingInputError("missing PyInstaller package")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in members.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return {name: fact(content) for name, content in sorted(members.items())}


def tree_inventory(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or path.is_junction():
            raise PackagingInputError("reparse member in packaging inventory")
        if path.is_file():
            name = safe_relative(path.relative_to(root).as_posix())
            if name.casefold() in {key.casefold() for key in result}:
                raise PackagingInputError("duplicate packaging inventory")
            result[name] = fact(path.read_bytes())
    return result


def dependency_members(site_packages):
    """Read only the declared import roots, without executing any package."""
    source = Path(site_packages)
    members = {}
    for modules, pattern in DEPENDENCIES:
        metadata = list(source.glob(pattern))
        if len(metadata) != 1:
            raise PackagingInputError("missing or ambiguous build dependency: " + pattern)
        for path in (*[source / module for module in modules], metadata[0]):
            if path.is_symlink() or path.is_junction():
                raise PackagingInputError("reparse build dependency")
            if path.is_dir():
                # Compiled caches are neither imported nor part of this source
                # snapshot. Reject links instead of following them while copying.
                for child in path.rglob("*"):
                    if child.is_symlink() or child.is_junction():
                        raise PackagingInputError("reparse build dependency member")
                paths = [child for child in path.rglob("*") if child.is_file()
                         and "__pycache__" not in child.relative_to(path).parts
                         and child.suffix.lower() not in {".pyc", ".pyo"}]
            elif path.is_file():
                paths = [path]
            else:
                raise PackagingInputError("missing build dependency: " + path.name)
            for child in paths:
                name = safe_relative(child.relative_to(source).as_posix())
                if name.casefold() in {key.casefold() for key in members}:
                    raise PackagingInputError("duplicate build dependency member")
                members[name] = child.read_bytes()
    return members


def copy_dependencies(site_packages, destination):
    members = dependency_members(site_packages)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, content in members.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return tree_inventory(destination)


def inventory_fact(inventory):
    return {"algorithm": "localcat.packaging-file-inventory.v1",
            "file_count": len(inventory), "bytes": sum(item["bytes"] for item in inventory.values()),
            "sha256": fact(canonical(inventory))["sha256"]}


def candidate_packaging_fact(repository, dependency_source):
    repository = Path(repository)
    members = dependency_members(dependency_source)
    return {"schema": "localcat.windows-frozen-packaging-target.v1",
            "source_files": {name: fact((repository / name).read_bytes()) for name in BUILD_SOURCE_FILES},
            "dependency_inventory": inventory_fact({name: fact(data) for name, data in members.items()}),
            "application_manifest": fact(APPLICATION_MANIFEST),
            "source_date_epoch": 0, "layout": "onedir-windowed-retained-source",
            "carchive": {"entries": [], "options": []}}


def require_candidate_dependencies(inventory, target):
    if inventory_fact(inventory) != target.get("dependency_inventory"):
        raise PackagingInputError("copied dependencies differ from candidate input")


def release_inventory(root, expected_payload):
    actual = tree_inventory(root)
    if set(actual) != set(expected_payload) | {"localcat-spike.exe"}:
        raise PackagingInputError("packaged distribution inventory mismatch")
    for name, expected in expected_payload.items():
        if actual[name] != expected:
            raise PackagingInputError("packaged payload bytes changed: " + name)
    return actual


def prepare_packaging(directory, *, archive, sdist_fact, runw, bundle, entries, dependency_source,
                      candidate_digest, packaging_target):
    """Snapshot inputs before child execution; return config and input facts."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    package_root = directory / "package"
    extract_packager(archive, sdist_fact, package_root)
    installed = package_root / "PyInstaller/bootloader/Windows-64bit-intel/runw.exe"
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(runw, installed)
    deps = directory / "dependencies"
    dependency_facts = copy_dependencies(dependency_source, deps)
    require_candidate_dependencies(dependency_facts, packaging_target)
    toc = [(entry.path, "payload/" + entry.path, "BINARY" if entry.role == "native" else "DATA")
           for entry in entries]
    toc.append(("localcat-runtime.manifest", "payload/localcat-runtime.manifest", "DATA"))
    spec_bytes = render_spec(sorted(toc)).encode("utf-8")
    payload = {}
    for entry in entries:
        actual = (Path(bundle) / entry.path).read_bytes()
        if actual != entry.content:
            raise PackagingInputError("pre-packaging payload drift: " + entry.path)
        target = directory / "payload" / entry.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(actual)
        payload[entry.path] = fact(actual)
    runtime_manifest = (Path(bundle) / "localcat-runtime.manifest").read_bytes()
    (directory / "payload/localcat-runtime.manifest").write_bytes(runtime_manifest)
    payload["localcat-runtime.manifest"] = fact(runtime_manifest)
    spec = directory / "localcat-spike.spec"
    spec.write_bytes(spec_bytes)
    driver = directory / "windows_frozen_packaging.py"
    shutil.copyfile(__file__, driver)
    if fact(driver.read_bytes()) != packaging_target["source_files"]["tools/windows_frozen_packaging.py"]:
        raise PackagingInputError("copied packaging driver differs from candidate input")
    inputs = {"schema": "localcat.windows-frozen-packaging-inputs.v1",
              "classification": "CANDIDATE_BOUND_PACKAGING_INPUTS_NOT_W3_APPROVAL",
              "candidate_input_digest": candidate_digest,
              "packaging_target_digest": fact(canonical(packaging_target))["sha256"],
              "sdist": {key: sdist_fact[key] for key in ("bytes", "sha256")},
              "packager": tree_inventory(package_root), "dependencies": dependency_facts,
              "driver": fact(driver.read_bytes()), "spec": fact(spec.read_bytes()),
              "payload": payload, "source_date_epoch": 0}
    (directory / "inputs.json").write_bytes(canonical(inputs))
    return driver, inputs


def load_packaging_inputs(directory, expected_digest):
    data = (Path(directory) / "inputs.json").read_bytes()
    if fact(data)["sha256"] != expected_digest:
        raise PackagingInputError("packaging input anchor mismatch")
    return json.loads(data)


def verify_packaging_result(directory, inputs):
    directory = Path(directory)
    result = json.loads((directory / "result.json").read_bytes())
    if result.get("inputs_digest") != fact(canonical(inputs))["sha256"]:
        raise PackagingInputError("packaging result anchor mismatch")
    actual = release_inventory(directory / "dist/localcat-spike", inputs["payload"])
    if result.get("dist") != actual or result.get("carchive") != {"entries": [], "options": []}:
        raise PackagingInputError("packaging result inventory mismatch")
    return result


def packaging_child(directory, expected_digest):
    """Called only by the isolated -I -S build child, before importing PyInstaller."""
    if not sys.flags.isolated or not sys.flags.no_site or os.name != "nt":
        raise PackagingInputError("packaging child requires isolated Windows Python without site")
    directory = Path(directory).resolve()
    inputs = load_packaging_inputs(directory, expected_digest)
    for name, key in (("package", "packager"), ("dependencies", "dependencies"), ("payload", "payload")):
        if tree_inventory(directory / name) != inputs[key]:
            raise PackagingInputError("packaging child input drift: " + name)
    if fact(Path(__file__).read_bytes()) != inputs["driver"]:
        raise PackagingInputError("packaging child driver drift")
    spec = directory / "localcat-spike.spec"
    if fact(spec.read_bytes()) != inputs["spec"]:
        raise PackagingInputError("packaging child spec drift")
    # No repository, CWD, user site, optional hooks, or ambient installed package.
    base = Path(sys.base_prefix).resolve()
    sys.path[:] = [str(directory / "package"), str(directory / "dependencies"),
                   str(base / "Lib"), str(base / "DLLs")]
    os.environ["SOURCE_DATE_EPOCH"] = str(inputs["source_date_epoch"])
    os.environ["PYINSTALLER_STRICT_COLLECT_MODE"] = "1"
    os.environ["PYINSTALLER_CONFIG_DIR"] = str(directory / "cache")
    from PyInstaller.building.build_main import main
    main({"cachedir": str(directory / "cache"), "upx_available": False}, str(spec),
         distpath=str(directory / "dist"), workpath=str(directory / "work"),
         noconfirm=False, clean_build=False)
    dist = directory / "dist/localcat-spike"
    inventory = release_inventory(dist, inputs["payload"])
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(dist / "localcat-spike.exe"))
    if archive.toc or archive.options:
        raise PackagingInputError("unexpected executable input in appended CArchive")
    result = {"schema": "localcat.windows-frozen-packaging-result.v1",
              "inputs_digest": hashlib.sha256(canonical(inputs)).hexdigest(),
              "dist": inventory, "carchive": {"entries": [], "options": []},
              "application_manifest": fact(APPLICATION_MANIFEST),
              "classification": "PACKAGING_COMPONENT_NOT_W3_GATE"}
    (directory / "result.json").write_bytes(canonical(result))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("expected the private packaging directory and input digest")
    packaging_child(sys.argv[1], sys.argv[2])
