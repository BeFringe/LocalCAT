"""Stock PyInstaller build; owner inputs are data or digests, never a loader."""
from __future__ import annotations

import argparse
import ast
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from frozen_candidate import INPUT_RECORD, canonical_json, write_candidate_record, load_candidate


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_environment(environment: dict[str, str]) -> dict[str, str]:
    # DLL analysis must not collect a same-named library from development tools
    # (for example Poppler's ICU instead of the Windows ICU used by Qt).
    clean = {key: value for key, value in environment.items()
             if key.upper() != "PATH" and not key.upper().startswith(("PYTHON", "QT_", "QML", "_PYI", "PYINSTALLER"))}
    system_root = next(value for key, value in clean.items() if key.upper() == "SYSTEMROOT")
    clean["PATH"] = os.pathsep.join(map(str, (Path(sys.executable).parent, Path(sys.base_prefix),
                                             Path(system_root) / "System32", Path(system_root))))
    return clean


def assignment(path: Path, name: str):
    def value(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left) + value(node.right)
        return ast.literal_eval(node)
    for node in ast.parse(path.read_bytes()).body:
        if isinstance(node, ast.Assign) and any(isinstance(n, ast.Name) and n.id == name for n in node.targets):
            return value(node.value)
    raise ValueError(f"owner declaration unavailable: {name}")


def collect_inputs(root: Path) -> tuple[dict[str, str], set[str], set[str]]:
    declaration = json.loads((root / "packaging/windows/frozen_roots.json").read_bytes())
    inputs: set[str] = {"frozen_ordinary_entry.py"}
    data: set[str] = set()
    modules = {"frozen_candidate", "frozen_worker_entry", "frozen_worker_transport", "frozen_product_entry"}
    for owner in declaration["owners"]:
        modules.update(owner.get("module_roots", []))
        modules.update(owner.get("runtime_import_roots", []))
        for item in owner.get("assets", []) + owner.get("gate_input_roots", []):
            data.add(item["path"])
        inventory = owner.get("source_inventory")
        if inventory:
            inputs.update(assignment(root / inventory["source"], inventory["symbol"]))
        for item in owner.get("dynamic_imports", []):
            names = item.get("modules")
            if names is None:
                names = assignment(root / item["source"], item["symbol"])
                if isinstance(names, str):
                    names = [names]
            modules.update(names)
        dispatch = owner.get("worker_dispatch")
        if dispatch:
            modules.update(assignment(root / dispatch["source"], dispatch["symbol"]).values())

    def gate_paths(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.endswith("_paths"):
                    inputs.update(item)
                elif key == "evaluator_path":
                    inputs.add(item)
                else:
                    gate_paths(item)
    for name in tuple(data):
        if name.endswith(".json"):
            gate_paths(json.loads((root / name).read_bytes()))
    data.update(name for name in inputs if not name.endswith(".py"))
    inputs.update(data)
    inputs.update(name.replace(".", "/") + ".py" for name in modules
                  if (root / (name.replace(".", "/") + ".py")).is_file())
    return {name: digest(root / name) for name in sorted(inputs)}, data, modules


def build(output: Path) -> Path:
    if sys.platform != "win32" or sys.version_info[:3] != (3, 14, 7) or platform.machine() != "AMD64":
        raise RuntimeError("ordinary build requires CPython 3.14.7 Windows x64")
    for line in (ROOT / "requirements-frozen-build.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            package, expected = line.split()[0].split("==")
            if version(package) != expected:
                raise RuntimeError("build dependency differs from the lock: " + package)
    status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT)
    if status.strip():
        raise RuntimeError("ordinary build requires a clean tracked tree")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    output.mkdir(parents=True, exist_ok=True)
    inputs, data, modules = collect_inputs(ROOT)
    # This data file is finalized with actual collected runtime facts below.
    inputs_file = output / INPUT_RECORD
    inputs_file.write_bytes(canonical_json({}))
    datas = [(str(ROOT / name), str(Path(name).parent).replace("\\", "/")) for name in sorted(data)]
    datas.append((str(inputs_file), "."))
    spec = output / "LocalCAT.spec"
    toc = output / "analysis-inputs.json"
    spec.write_text(
        "import json\nfrom pathlib import Path\n"
        f"a = Analysis([{str(ROOT / 'frozen_ordinary_entry.py')!r}], pathex=[{str(ROOT)!r}], "
        f"binaries=[], datas={datas!r}, hiddenimports={sorted(modules)!r}, "
        "hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=['xlwings', 'tkinter'], noarchive=False, optimize=0)\n"
        f"Path({str(toc)!r}).write_text(json.dumps(dict(pure=list(a.pure), scripts=list(a.scripts), binaries=list(a.binaries), datas=list(a.datas))), encoding='utf-8')\n"
        "pyz = PYZ(a.pure)\n"
        f"exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='LocalCAT', debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=False, disable_windowed_traceback=True, icon={str(ROOT / 'LocalCAT-logo-silver.ico')!r})\n"
        "coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='LocalCAT')\n",
        encoding="utf-8",
    )
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(output / "dist"),
                    "--workpath", str(output / "work"), str(spec)], cwd=ROOT,
                   env=build_environment(dict(os.environ)), check=True)
    dist = output / "dist/LocalCAT"
    analysis = json.loads(toc.read_text(encoding="utf-8"))
    pure = {name: source for name, source, _kind in analysis["pure"]}
    missing = {name for name in modules if (ROOT / (name + ".py")).is_file() and name not in pure}
    if missing:
        raise RuntimeError("owner modules missing from PYZ: " + repr(sorted(missing)))
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(dist / "LocalCAT.exe"))
    pyz_name = next(name for name in archive.toc if name.endswith(".pyz"))
    collected = set(archive.open_embedded_archive(pyz_name).toc)
    mapping = {}
    for name, source in pure.items():
        path = Path(source)
        if path.is_relative_to(ROOT):
            if name not in collected:
                raise RuntimeError("project owner module absent from actual PYZ: " + name)
            relative = path.relative_to(ROOT).as_posix()
            mapping[relative] = {"sha256": digest(path), "output": "LocalCAT.exe/" + pyz_name + "/" + name}
    for name, source, _kind in analysis["scripts"]:
        path = Path(source)
        if path.is_relative_to(ROOT):
            if name not in archive.toc:
                raise RuntimeError("entry script absent from actual executable")
            mapping[path.relative_to(ROOT).as_posix()] = {"sha256": digest(path), "output": "LocalCAT.exe/" + name}
    for name in data:
        if digest(dist / "_internal" / name) != inputs[name]:
            raise RuntimeError("collected owner data differs: " + name)
        mapping[name] = {"sha256": inputs[name], "output": "_internal/" + name}
    runtime_files = sorted(path.relative_to(dist).as_posix() for path in (dist / "_internal").rglob("*")
                           if path.is_file() and path.name.lower() in {"python314.dll", "sqlite3.dll", "_sqlite3.pyd"})
    execution_inputs = ("frozen_candidate.py", "frozen_ordinary_entry.py", "frozen_worker_entry.py", "frozen_worker_transport.py")
    execution = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                 "pyinstaller": version("pyinstaller"), "collection": "pyz", "optimization": 0,
                 "worker_inputs": {name: digest(ROOT / name) for name in execution_inputs},
                 "runtime_files": {name: digest(dist / name) for name in runtime_files}}
    record = {"schema": "localcat-ordinary-inputs-v1", "input_digests": inputs,
              "data_ids": sorted(data), "core_execution": execution, "runtime_files": runtime_files}
    (dist / "_internal" / INPUT_RECORD).write_bytes(canonical_json(record))
    # Verify the source did not move while Analysis compiled it.
    if collect_inputs(ROOT)[0] != inputs or subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip():
        raise RuntimeError("build inputs changed during collection")
    write_candidate_record(dist, {"repository_commit": commit, "recipe_sha256": digest(spec),
                                 "dependency_lock_sha256": digest(ROOT / "requirements-frozen-build.txt"),
                                 "owner_outputs": mapping})
    load_candidate(dist / "LocalCAT.exe", verify_payload=True)
    return dist / "LocalCAT.exe"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/windows/ordinary-build")
    print(build(parser.parse_args().output.resolve()))
