"""Discover source bytes without importing application code.

Collection/provenance only: Core owns Gate semantics and the native producer
owns runtime proof. Development manifests cannot satisfy production validation.
"""
from __future__ import annotations

import argparse
import ast
import base64
import csv
from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Iterable
import zipfile

ROOTS_PATH = "packaging/windows/frozen_roots.json"
GENERATOR_PATH = "tools/generate_windows_frozen_manifest.py"
BUILD_PLAN_PATH = "packaging/windows/build_inputs.json"
SCHEMA = "localcat-frozen-source-collection-v1"
BUILD_SCHEMA = "localcat-frozen-build-inputs-v1"
_MANDATORY_BUILD_FILES = {
    "packaging/windows/LocalCAT.spec": "spec",
    "frozen_source_bootstrap.py": "bootstrap",
    "packaging/windows/LocalCAT.ico": "icon",
    "packaging/windows/version_info.txt": "version",
    "packaging/windows/requirements-build.lock": "requirements-lock",
}
_TREE_KINDS = {"locked-wheels", "python-runtime", "native-runtime", "bootloader", "build-toolchain"}


class ManifestError(ValueError):
    """A missing, ambiguous, changed or unauthorized collection input."""


def canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def fact(content: bytes) -> dict[str, Any]:
    return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def ordered(values: Iterable[str]) -> list[str]:
    return sorted(set(values), key=lambda value: value.encode("utf-8"))


def relative(value: Any) -> str:
    if type(value) is not str or not value or "\\" in value:
        raise ManifestError(f"invalid relative path: {value!r}")
    try:
        if len(value.encode("utf-8")) > 1024:
            raise ManifestError(f"oversized path: {value!r}")
        for part in value.split("/"):
            if not part or part in {".", ".."} or part.endswith((".", " ")):
                raise ManifestError(f"invalid path: {value!r}")
            if len(part.encode("utf-16-le")) > 510 or any(ord(char) < 32 or char in '<>:"|?*' for char in part):
                raise ManifestError(f"unsafe Windows path: {value!r}")
            base = part.split(".", 1)[0].upper()
            if base in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9\u00b9\u00b2\u00b3]", base):
                raise ManifestError(f"reserved Windows path: {value!r}")
            if part.casefold() == "__pycache__":
                raise ManifestError(f"bytecode path: {value!r}")
    except UnicodeError as exc:
        raise ManifestError("invalid Unicode path") from exc
    if value.lower().endswith((".pyc", ".pyo", ".pyz")):
        raise ManifestError(f"bytecode path: {value}")
    lowered = value.casefold()
    if any(token in lowered for token in ("durability_profiles", "power-loss", "power_loss", "power-evidence")):
        raise ManifestError(f"forbidden durability/power input: {value}")
    return value


def _object(value: Any, required: set[str], optional: set[str], label: str) -> dict[str, Any]:
    if type(value) is not dict or not required <= value.keys() or value.keys() - required - optional:
        raise ManifestError(f"missing or unknown {label} fields")
    return value


def _strings(value: Any, label: str, *, nonempty: bool = False) -> list[str]:
    if type(value) is not list or any(type(item) is not str or not item for item in value) or len(value) != len(set(value)) or (nonempty and not value):
        raise ManifestError(f"invalid {label}")
    return value


def _json(content: bytes, label: str) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ManifestError(f"duplicate JSON member in {label}: {key}")
            result[key] = value
        return result
    try:
        value = json.loads(content, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise ManifestError(f"invalid JSON: {label}: {exc}") from exc
    if type(value) is not dict:
        raise ManifestError(f"expected JSON object: {label}")
    return value


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True)
    if result.returncode:
        raise ManifestError(f"Git input query failed: {' '.join(args)}")
    return result.stdout


def _read(root: Path, name: str) -> bytes:
    relative(name)
    for ancestor in (root, *root.parents):
        info = ancestor.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ManifestError(f"reparse input ancestor: {name}")
    current = root
    for part in name.split("/"):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ManifestError(f"missing input: {name}") from exc
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ManifestError(f"reparse input: {name}")
    if not current.is_file():
        raise ManifestError(f"input is not a file: {name}")
    before = current.stat()
    content = current.read_bytes()
    after = current.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ManifestError(f"input changed during read: {name}")
    return content


class Repository:
    def __init__(self, root: Path, *, production: bool):
        self.root = root.absolute()
        if Path(_git(self.root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != self.root.resolve():
            raise ManifestError("repository root must be the Git worktree root")
        self.commit = _git(self.root, "rev-parse", "HEAD").decode().strip()
        self.tracked = {item.decode("utf-8") for item in _git(self.root, "ls-files", "-z").split(b"\0") if item}
        self.production = production
        self.contents: dict[str, bytes] = {}
        self.dirty: set[str] = set()
        if production:
            self.require_clean()

    def require_clean(self) -> None:
        if _git(self.root, "status", "--porcelain=v1", "--untracked-files=no"):
            raise ManifestError("production requires a clean tracked tree")

    def read(self, name: str) -> bytes:
        relative(name)
        if name not in self.tracked:
            raise ManifestError(f"untracked actual input: {name}")
        content = _read(self.root, name)
        if name in self.contents and content != self.contents[name]:
            raise ManifestError(f"input drift: {name}")
        if name in self.contents:
            return content
        self.contents[name] = content
        original = _git(self.root, "show", f"HEAD:{name}")
        if content != original:
            self.dirty.add(name)
            if self.production:
                raise ManifestError(f"dirty actual input: {name}")
        return content

    def reprove(self) -> None:
        if _git(self.root, "rev-parse", "HEAD").decode().strip() != self.commit:
            raise ManifestError("repository commit changed")
        for name, content in self.contents.items():
            if _read(self.root, name) != content:
                raise ManifestError(f"input drift: {name}")
        if self.production:
            self.require_clean()


def _literal(node: ast.AST, assignments: dict[str, ast.AST], active: frozenset[str] = frozenset()) -> Any:
    if isinstance(node, ast.Name) and node.id in assignments and node.id not in active:
        return _literal(assignments[node.id], assignments, active | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, assignments, active), _literal(node.right, assignments, active)
        if type(left) is type(right) and isinstance(left, (str, tuple, list)):
            return left + right
    if isinstance(node, (ast.Tuple, ast.List)):
        result = [_literal(item, assignments, active) for item in node.elts]
        return tuple(result) if isinstance(node, ast.Tuple) else result
    if isinstance(node, ast.Dict):
        mapping: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            item = _literal(value, assignments, active)
            if key is None:
                if type(item) is not dict:
                    raise ManifestError("dictionary unpack is not a proven literal mapping")
                mapping.update(item)
            else:
                try:
                    mapping[_literal(key, assignments, active)] = item
                except TypeError as exc:
                    raise ManifestError("dictionary key is not a hashable literal") from exc
        return mapping
    if isinstance(node, ast.Constant) and type(node.value) in {str, int, bool, type(None)}:
        return node.value
    raise ManifestError("nonliteral or cyclic declaration")


def _assignments(tree: ast.Module, bindings: _ScopeBindings | None = None) -> dict[str, ast.AST]:
    bindings = bindings or _ScopeBindings(tree)
    result: dict[str, ast.AST] = {}
    for node in tree.body:
        pairs: list[tuple[ast.expr, ast.expr]] = []
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        for target, value in pairs:
            if isinstance(target, ast.Name):
                result[target.id] = ast.Pass() if target.id in result else value
    primary = {id(target) for node in tree.body if isinstance(node, (ast.Assign, ast.AnnAssign)) for target in (node.targets if isinstance(node, ast.Assign) else [node.target]) if isinstance(target, ast.Name)}
    for name in result:
        if bindings.uncertain(tree, name) or any(id(node) not in primary for node in bindings.records.get((tree, name), [])):
            result[name] = ast.Pass()
    return result


_FUNCTION_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSION_SCOPES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPES = (ast.Module, ast.ClassDef, ast.TypeAlias, *_FUNCTION_SCOPES, *_COMPREHENSION_SCOPES)


def _enclosing_scopes(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> list[ast.AST]:
    scopes: list[ast.AST] = []
    child = node
    ancestry = {node}
    while child in parents:
        parent = parents[child]
        if isinstance(parent, _SCOPES):
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                included = child in parent.body or isinstance(node, ast.arg) or child in parent.type_params
            elif isinstance(parent, ast.Lambda):
                included = child is parent.body or isinstance(node, ast.arg)
            elif isinstance(parent, _COMPREHENSION_SCOPES):
                # The first iterable executes in the enclosing lexical scope.
                included = parent.generators[0].iter not in ancestry
            elif isinstance(parent, ast.TypeAlias):
                included = child is not parent.name
            else:
                included = True
            if included:
                scopes.append(parent)
        child = parent
        ancestry.add(parent)
    return scopes


def _node_bindings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
        return [node.id]
    if isinstance(node, ast.arg):
        return [node.arg]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name) for alias in node.names]
    if isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
        return [node.name]
    if isinstance(node, ast.MatchMapping) and node.rest:
        return [node.rest]
    if isinstance(node, (ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple)):
        return [node.name]
    return []


class _ScopeBindings:
    """One binding inventory for literal targets and import callable identity.

    Every binding node is routed through its lexical block's global/nonlocal
    declarations, even when the write occurs in a different nested function.
    This proves bounded static declarations; it never evaluates application code.
    """

    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self.scopes = {node: _enclosing_scopes(node, self.parents) for node in ast.walk(tree)}
        self.local: dict[ast.AST, set[str]] = {}
        self.global_names: dict[ast.AST, set[str]] = {}
        self.nonlocal_names: dict[ast.AST, set[str]] = {}
        self.records: dict[tuple[ast.AST, str], list[ast.AST]] = {}
        self.mutated: set[tuple[ast.AST, str]] = set()
        self.reflective = False
        for node in ast.walk(tree):
            scope = self.lexical_owner(node)
            self.local.setdefault(scope, set()).update(_node_bindings(node))
            if isinstance(node, ast.Global):
                self.global_names.setdefault(scope, set()).update(node.names)
            elif isinstance(node, ast.Nonlocal):
                self.nonlocal_names.setdefault(scope, set()).update(node.names)
        for node in ast.walk(tree):
            for name in _node_bindings(node):
                owner = self.effective_owner(self.lexical_owner(node), name)
                self.records.setdefault((owner, name), []).append(node)
        aliases: dict[tuple[ast.AST, str], set[tuple[ast.AST, str]]] = {}
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) else []
            value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) else None
            if isinstance(value, ast.Name):
                source = (self.lookup(value, value.id), value.id)
                for target in targets:
                    if isinstance(target, ast.Name):
                        destination = (self.effective_owner(self.lexical_owner(target), target.id), target.id)
                        aliases.setdefault(source, set()).add(destination)
                        aliases.setdefault(destination, set()).add(source)
            target: ast.expr | None = None
            if isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
                target = node.value
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr not in {"get", "keys", "values", "items", "count", "index", "copy", "import_module"}:
                target = node.func.value
            while isinstance(target, (ast.Subscript, ast.Attribute)):
                target = target.value
            if isinstance(target, ast.Name):
                self.mutated.add((self.lookup(target, target.id), target.id))
            if isinstance(target, ast.Call) and isinstance(target.func, ast.Name) and target.func.id in {"globals", "locals", "vars"} and not target.args:
                self.reflective = True
        pending = list(self.mutated)
        while pending:
            for alias in aliases.get(pending.pop(), set()) - self.mutated:
                self.mutated.add(alias)
                pending.append(alias)

    def lexical_owner(self, node: ast.AST) -> ast.AST:
        scopes = self.scopes[node]
        parent = self.parents.get(node)
        if isinstance(parent, ast.NamedExpr) and parent.target is node:
            # A comprehension walrus binds its enclosing non-comprehension block.
            scopes = [scope for scope in scopes if not isinstance(scope, _COMPREHENSION_SCOPES)]
        return scopes[0] if scopes else self.tree

    def effective_owner(self, scope: ast.AST, name: str) -> ast.AST:
        if name in self.global_names.get(scope, set()):
            return self.tree
        if name in self.nonlocal_names.get(scope, set()):
            for parent in self.scopes[scope]:
                if isinstance(parent, _FUNCTION_SCOPES) and name in self.local.get(parent, set()) and name not in self.global_names.get(parent, set()) | self.nonlocal_names.get(parent, set()):
                    return parent
            raise ManifestError(f"unproven nonlocal binding: {name}")
        return scope

    def lookup(self, node: ast.AST, name: str) -> ast.AST:
        scopes = self.scopes[node]
        for position, scope in enumerate(scopes):
            # Methods and comprehensions do not close over a class namespace.
            if position and isinstance(scope, ast.ClassDef):
                continue
            if name in self.global_names.get(scope, set()) | self.nonlocal_names.get(scope, set()):
                return self.effective_owner(scope, name)
            if name in self.local.get(scope, set()):
                return scope
        return self.tree

    def uncertain(self, scope: ast.AST, name: str) -> bool:
        return self.reflective or (scope, name) in self.mutated or (scope, "*") in self.records

    def require_supported_scope(self, node: ast.AST) -> None:
        child = node
        while child in self.parents:
            parent = self.parents[child]
            annotation = isinstance(parent, (ast.TypeAlias, ast.TypeVar, ast.ParamSpec, ast.TypeVarTuple))
            annotation |= isinstance(parent, (ast.arg, ast.AnnAssign)) and child is parent.annotation
            annotation |= isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is parent.returns
            annotation |= isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and bool(parent.type_params)
            if annotation:
                raise ManifestError("unproven dynamic annotation scope")
            child = parent


def _scoped_literal(node: ast.AST, assignments: dict[str, ast.AST], bindings: _ScopeBindings) -> Any:
    bindings.require_supported_scope(node)
    names = {item.id for item in ast.walk(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
    overlap = {name for name in names if bindings.lookup(node, name) is not bindings.tree}
    if overlap:
        raise ManifestError("shadowed dynamic binding: " + ", ".join(ordered(overlap)))
    return _literal(node, assignments)


def _require_import_binding(name: str, call: ast.Call, bindings: _ScopeBindings, *, function: bool) -> None:
    bindings.require_supported_scope(call)
    scope = bindings.lookup(call, name)
    nodes = bindings.records.get((scope, name), [])
    matching = []
    for node in nodes:
        if isinstance(node, ast.Import) and not function:
            matching.extend(alias for alias in node.names if alias.name == "importlib" and (alias.asname or alias.name) == name)
        elif isinstance(node, ast.ImportFrom) and function and node.module == "importlib" and node.level == 0:
            matching.extend(alias for alias in node.names if alias.name == "import_module" and (alias.asname or alias.name) == name)
    if len(matching) == len(nodes) == 1 and not bindings.uncertain(scope, name):
        return
    raise ManifestError(f"shadowed dynamic callable binding: {name}")


def _module_name(path: str) -> str:
    return path[:-3].replace("/", ".").removesuffix(".__init__")


class Collector:
    def __init__(self, repository: Repository):
        self.repo = repository
        self.entries: dict[str, dict[str, Any]] = {}
        self.trees: dict[str, ast.Module] = {}
        self.pending: list[str] = []
        self.scanned: set[str] = set()
        self.declared: set[str] = set()
        self.dynamic: dict[str, dict[str, set[str]]] = {}
        self.external_roots: set[str] = set()
        self.external: dict[str, set[str]] = {}
        self.calls: list[dict[str, Any]] = []
        self.plugins: set[str] = set()

    def add(self, path: str, kind: str, reason: str) -> None:
        path = relative(path)
        if type(reason) is not str or not reason:
            raise ManifestError(f"missing root reason: {path}")
        if path.endswith(".py"):
            kind = "critical-source"
        if path not in self.entries:
            if any(other.casefold() == path.casefold() for other in self.entries):
                raise ManifestError(f"duplicate Windows path: {path}")
            self.entries[path] = {"path": path, "kind": kind, **fact(self.repo.read(path)), "reasons": set(), "dependencies": set()}
            if kind == "critical-source":
                self.entries[path]["module"] = _module_name(path)
            if kind == "critical-source" or path.endswith(".spec"):
                self.pending.append(path)
        elif self.entries[path]["kind"] != kind:
            raise ManifestError(f"conflicting kinds: {path}")
        self.entries[path]["reasons"].add(reason)

    def tree(self, path: str) -> ast.Module:
        if path not in self.trees:
            try:
                self.trees[path] = ast.parse(self.repo.read(path), filename=path)
            except (SyntaxError, UnicodeError) as exc:
                raise ManifestError(f"invalid Python input: {path}") from exc
        return self.trees[path]

    def constant(self, path: str, name: str) -> Any:
        values = _assignments(self.tree(path))
        if name not in values:
            raise ManifestError(f"missing owner symbol: {path}:{name}")
        return _literal(values[name], values, frozenset({name}))

    def module(self, name: str, reason: str, *, source: str | None = None, required: bool = True) -> None:
        if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", name) is None:
            raise ManifestError(f"invalid module name: {name!r}")
        parts = name.split(".")
        found: list[str] = []
        exact = False
        for index in range(1, len(parts) + 1):
            base = "/".join(parts[:index])
            candidates = [value for value in (base + ".py", base + "/__init__.py") if value in self.repo.tracked or (self.repo.root / value).exists()]
            if len(candidates) > 1:
                raise ManifestError(f"ambiguous module: {name}")
            if candidates:
                found.append(candidates[0])
                exact = index == len(parts)
        namespace = any(path.startswith(name.replace(".", "/") + "/") for path in self.repo.tracked)
        if found:
            if required and not exact and not namespace:
                raise ManifestError(f"unknown project dependency: {name}")
            for path in found:
                self.add(path, "critical-source", reason)
                if source is not None:
                    self.entries[source]["dependencies"].add(path)
            return
        if namespace:
            return
        if not required:
            return
        if parts[0] in sys.stdlib_module_names or parts[0] in {"__future__", "_frozen_importlib", "_frozen_importlib_external"} or parts[0] in {value.split(".")[0] for value in self.external_roots}:
            self.external.setdefault(name, set()).add(source or reason)
            return
        raise ManifestError(f"unknown dependency: {name} from {source or reason}")

    def _gate(self, path: str) -> None:
        # Project only owner path references; never validate or re-sign their
        # semantic digest/fingerprint. Runtime uses the original Core validators.
        value = _json(self.repo.read(path), path)
        schema = value.get("schema_version")
        if schema == "feature5-gate-a-v1":
            _object(value, {"schema_version", "components", "matcher"}, set(), path)
            if type(value["components"]) is not dict or not value["components"]:
                raise ManifestError(f"invalid Gate components: {path}")
            groups = list(value["components"].items()) + [("matcher", value["matcher"])]
        elif schema == "retrieval-gate-c-roots-v1":
            groups = [("retrieval", value)]
        else:
            raise ManifestError(f"unknown Gate roots schema: {path}")
        for label, group in groups:
            if type(group) is not dict:
                raise ManifestError(f"invalid Gate path group: {path}:{label}")
            required = {"artifact_paths", "fixture_paths"}
            if label in {"matcher", "retrieval"}:
                required |= {"build_paths", "evaluator_path"}
            if not required <= group.keys():
                raise ManifestError(f"missing Gate path fields: {path}:{label}")
            for key, item in group.items():
                if key in {"artifact_paths", "build_paths", "fixture_paths"}:
                    for name in _strings(item, f"{path}:{key}"):
                        self.add(name, "fixture" if key == "fixture_paths" else "critical-source", f"gate:{path}:{label}:{key}")
                        if name.endswith(".py"):
                            self.declared.add(_module_name(name))
                elif key == "evaluator_path":
                    self.add(item, "critical-source", f"gate:{path}:{label}:evaluator_path")
                    self.declared.add(_module_name(item))
                elif key.endswith(("_path", "_paths")):
                    raise ManifestError(f"unknown Gate path field: {path}:{key}")

    def roots(self) -> None:
        self.add(ROOTS_PATH, "owner-roots", "approved-owner-declarations")
        value = _json(self.repo.read(ROOTS_PATH), ROOTS_PATH)
        _object(value, {"schema_version", "scope", "collection_policy", "owners", "platform"}, set(), "owner roots")
        if value["schema_version"] != "localcat-frozen-owner-roots-v1" or value["scope"] != "prebuild-owner-declarations":
            raise ManifestError("unknown owner roots schema/scope")
        policy = _object(value["collection_policy"], {"project_source_modules", "gate_inputs", "recursive_closure_owner", "native_bootstrap_owner"}, set(), "collection policy")
        if policy["project_source_modules"] != "py" or policy["gate_inputs"] != "preserve-repository-relative-layout":
            raise ManifestError("source-only collection required")
        if type(value["owners"]) is not list or not value["owners"]:
            raise ManifestError("owner declarations required")
        owners = [*value["owners"], value["platform"]]
        for owner in owners:
            _object(owner, {"owning_spec", "module_roots"}, {"dispatch_id", "revision", "reason", "assets", "gate_input_roots", "source_inventory", "dynamic_imports", "worker_dispatch", "runtime_import_roots", "qt_plugin_roots", "speaker_avatar_catalog"}, "owner")
            self.external_roots.update(_strings(owner.get("runtime_import_roots", []), "runtime roots"))
            self.plugins.update(relative(item) for item in _strings(owner.get("qt_plugin_roots", []), "Qt plugins"))
            for declaration in owner.get("dynamic_imports", []):
                _object(declaration, {"source", "phase", "reason"}, {"modules", "symbol"}, "dynamic import")
                if ("modules" in declaration) == ("symbol" in declaration):
                    raise ManifestError("dynamic import needs exactly modules or symbol")
                source = relative(declaration["source"])
                if declaration["phase"] not in {"post-authority", "post-E10"}:
                    raise ManifestError("unsupported dynamic import phase")
                raw = declaration.get("modules")
                if "symbol" in declaration:
                    raw = self.constant(source, declaration["symbol"])
                    raw = [raw] if type(raw) is str else list(raw) if type(raw) is tuple else raw
                modules = _strings(raw, "dynamic modules", nonempty=True)
                key = declaration.get("symbol", "<literal>")
                self.dynamic.setdefault(source, {}).setdefault(key, set()).update(modules)
                self.declared.update(modules)
                for name in modules:
                    if not any((self.repo.root / item).exists() for item in (name.replace(".", "/") + ".py", name.replace(".", "/") + "/__init__.py")):
                        self.external_roots.add(name)
        for owner in owners:
            label = str(owner.get("dispatch_id", owner["owning_spec"]))
            for name in _strings(owner["module_roots"], "module roots", nonempty=True):
                self.declared.add(name)
                self.module(name, f"owner:{label}:{owner.get('reason', 'platform composition')}")
            for field in ("assets", "gate_input_roots"):
                for asset in owner.get(field, []):
                    _object(asset, {"path", "kind", "reason"}, set(), field)
                    if asset["kind"] not in {"data", "asset", "contract", "gate-roots", "fixture"}:
                        raise ManifestError("unknown asset kind")
                    self.add(asset["path"], asset["kind"], f"owner:{label}:{asset['reason']}")
                    if asset["kind"] == "gate-roots":
                        self._gate(asset["path"])
            for field in ("source_inventory", "worker_dispatch"):
                if field not in owner:
                    continue
                declaration = _object(owner[field], {"source", "symbol", "reason"}, {"selector", "phase"}, field)
                source = relative(declaration["source"])
                self.add(source, "critical-source", f"owner:{label}:{field}")
                raw = self.constant(source, declaration["symbol"])
                if field == "source_inventory":
                    if type(raw) is not tuple or not raw or any(type(item) is not str for item in raw):
                        raise ManifestError("source inventory must be a literal tuple")
                    for path in raw:
                        self.add(path, "critical-source" if path.endswith(".py") else "contract", f"owner-inventory:{source}:{declaration['symbol']}")
                        if path.endswith(".py"):
                            self.declared.add(_module_name(path))
                else:
                    if type(raw) is not dict or not raw or any(type(key) is not str or type(item) is not str for key, item in raw.items()):
                        raise ManifestError("worker dispatch must be a literal mapping")
                    for name in raw.values():
                        self.declared.add(name)
                        self.module(name, f"owner-worker:{source}:{declaration['symbol']}", source=source)
        for source, declarations in self.dynamic.items():
            self.add(source, "critical-source", "owner-dynamic-declaration")
            for symbol, modules in declarations.items():
                for name in modules:
                    self.module(name, f"owner-dynamic:{source}:{symbol}", source=source)

    def _bounded_anchor_call(self, path: str, call: ast.Call, bindings: _ScopeBindings) -> set[str]:
        # Verify Core's concrete ordered anchor bound. A declaration in a module
        # must never permit unrelated unknown dynamic calls in that module.
        parents = bindings.parents
        argument = call.args[0]
        if not isinstance(argument, ast.Attribute) or argument.attr != "module_name" or not isinstance(argument.value, ast.Name):
            raise ManifestError(f"unknown dynamic import: {path}:{call.lineno}")
        parent = parents.get(call)
        if not isinstance(parent, ast.GeneratorExp) or len(parent.generators) != 1:
            raise ManifestError(f"unbounded dynamic import: {path}:{call.lineno}")
        comp = parent.generators[0]
        if not isinstance(comp.target, ast.Name) or comp.target.id != argument.value.id or not isinstance(comp.iter, ast.Name):
            raise ManifestError(f"unbounded dynamic import: {path}:{call.lineno}")
        function: ast.AST | None = parent
        while function is not None and not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = parents.get(function)
        if function is None:
            raise ManifestError(f"unbounded dynamic import: {path}:{call.lineno}")
        expected: ast.expr | None = None
        guards: list[ast.expr] = []
        guard_index = -1
        call_index = -1
        for index, node in enumerate(function.body):
            if any(child is call for child in ast.walk(node)):
                call_index = index
            if node.lineno >= call.lineno:
                break
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "expected_names":
                expected = node.value
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.NotEq) and len(node.test.comparators) == 1 and isinstance(node.test.comparators[0], ast.Name) and node.test.comparators[0].id == "expected_names" and len(node.body) == 1 and isinstance(node.body[0], ast.Raise):
                guards.append(node.test.left)
                guard_index = index
        expected_guard = ast.parse(f"tuple({argument.value.id}.module_name for {argument.value.id} in {comp.iter.id})", mode="eval").body
        if expected is None or call_index != guard_index + 1 or not any(ast.dump(guard) == ast.dump(expected_guard) for guard in guards):
            raise ManifestError(f"missing dynamic owner bound: {path}:{call.lineno}")
        constants = _assignments(self.tree(path), bindings)
        choices = [expected.body, expected.orelse] if isinstance(expected, ast.IfExp) else [expected]
        modules: set[str] = set()
        for choice in choices:
            result = _scoped_literal(choice, constants, bindings)
            if type(result) is not tuple or any(type(item) is not str for item in result):
                raise ManifestError(f"invalid dynamic owner bound: {path}:{call.lineno}")
            modules.update(result)
        if not modules <= self.declared:
            raise ManifestError(f"undeclared dynamic owner bound: {path}:{call.lineno}")
        return modules

    def scan(self) -> None:
        while self.pending:
            path = self.pending.pop()
            if path in self.scanned:
                continue
            self.scanned.add(path)
            tree = self.tree(path)
            bindings = _ScopeBindings(tree)
            parents = bindings.parents
            imports = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names if alias.name == "importlib"}
            functions = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "importlib" for alias in node.names if alias.name == "import_module"}
            constants = _assignments(tree, bindings)
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "__import__":
                    raise ManifestError(f"unsupported dynamic import alias: {path}:{node.lineno}")
                if isinstance(node, ast.Attribute) and node.attr == "import_module":
                    parent = parents.get(node)
                    if not isinstance(parent, ast.Call) or parent.func is not node or not isinstance(node.value, ast.Name) or node.value.id not in imports:
                        raise ManifestError(f"unsupported dynamic import alias: {path}:{node.lineno}")
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in functions | {"__import__"}:
                    parent = parents.get(node)
                    if not isinstance(parent, ast.Call) or parent.func is not node:
                        raise ManifestError(f"unsupported dynamic import alias: {path}:{node.lineno}")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in imports:
                    raise ManifestError(f"unsupported reflective dynamic import: {path}:{node.lineno}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.module(alias.name, f"import:{path}:{node.lineno}", source=path)
                elif isinstance(node, ast.ImportFrom):
                    package = path.split("/")[:-1]
                    if node.level:
                        if node.level > len(package):
                            raise ManifestError(f"relative import escape: {path}:{node.lineno}")
                        base = ".".join(package[:len(package) - node.level + 1] + ([node.module] if node.module else []))
                    else:
                        base = node.module or ""
                    if base:
                        self.module(base, f"import:{path}:{node.lineno}", source=path)
                        for alias in node.names:
                            if alias.name != "*":
                                self.module(base + "." + alias.name, f"import:{path}:{node.lineno}", source=path, required=False)
                elif isinstance(node, ast.Call):
                    is_dynamic = isinstance(node.func, ast.Name) and node.func.id in functions | {"__import__"}
                    is_dynamic = is_dynamic or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module" and isinstance(node.func.value, ast.Name) and node.func.value.id in imports)
                    if not is_dynamic:
                        continue
                    if len(node.args) != 1 or node.keywords:
                        raise ManifestError(f"unsupported dynamic import: {path}:{node.lineno}")
                    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                        _require_import_binding(node.func.value.id, node, bindings, function=False)
                    elif isinstance(node.func, ast.Name) and node.func.id != "__import__":
                        _require_import_binding(node.func.id, node, bindings, function=True)
                    elif isinstance(node.func, ast.Name) and (bindings.records.get((bindings.lookup(node, node.func.id), node.func.id)) or bindings.uncertain(bindings.lookup(node, node.func.id), node.func.id)):
                        raise ManifestError(f"shadowed dynamic callable binding: {node.func.id}")
                    try:
                        name = _scoped_literal(node.args[0], constants, bindings)
                    except ManifestError:
                        if not isinstance(node.args[0], ast.Attribute):
                            raise
                        names = self._bounded_anchor_call(path, node, bindings)
                    else:
                        if type(name) is not str:
                            raise ManifestError(f"non-string dynamic import: {path}:{node.lineno}")
                        names = {name}
                        if name not in self.declared and name.split(".")[0] not in sys.stdlib_module_names:
                            raise ManifestError(f"undeclared dynamic import: {path}:{node.lineno}:{name}")
                    self.calls.append({"source": path, "line": node.lineno, "modules": ordered(names)})
                    for name in names:
                        self.module(name, f"dynamic:{path}:{node.lineno}", source=path)


@dataclass(frozen=True)
class GeneratedManifest:
    manifest: dict[str, Any]
    manifest_bytes: bytes
    hook_bytes: bytes


def _hook(entries: list[dict[str, Any]]) -> bytes:
    modules = ordered(entry["module"] for entry in entries if entry["kind"] == "critical-source")
    return ("# Generated by generate_windows_frozen_manifest.py; do not edit.\n"
            "# Collection is not runtime executed-byte attestation.\n"
            "module_collection_mode = {\n" + "".join(f"    {name!r}: 'py',\n" for name in modules) + "}\n").encode("utf-8")


def generate(root: Path, *, mode: str = "production", input_roots: dict[str, Path] | None = None) -> GeneratedManifest:
    if mode not in {"production", "development"}:
        raise ManifestError("unknown generation mode")
    repo = Repository(root, production=mode == "production")
    collector = Collector(repo)
    collector.roots()
    collector.scan()
    entries = [{**value, "reasons": ordered(value["reasons"]), "dependencies": ordered(value["dependencies"])} for _, value in sorted(collector.entries.items(), key=lambda pair: pair[0].encode("utf-8"))]
    hook = _hook(entries)
    generator_bytes = Path(__file__).read_bytes()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA,
        "classification": "PRODUCTION_PRELINK_INPUTS" if mode == "production" else "DEVELOPMENT_CLOSURE_NOT_RELEASE",
        "repository_commit": repo.commit, "entries": entries,
        "project_graph": "python-import-graph-cycles-allowed",
        "dynamic_imports": sorted(collector.calls, key=lambda item: (item["source"].encode("utf-8"), item["line"])),
        "external_imports": [{"module": name, "classification": "stdlib-source-reference" if name.split(".")[0] in sys.stdlib_module_names else "owner-declared-runtime", "consumers": ordered(consumers)} for name, consumers in sorted(collector.external.items())],
        "runtime_import_roots": ordered(collector.external_roots), "qt_plugin_roots": ordered(collector.plugins),
        "generated_outputs": [{"path": "hooks/hook-capability_host.py", "kind": "generated-source-only-hook", **fact(hook)}],
        "generator": {"path": GENERATOR_PATH, **fact(generator_bytes)},
        "exclusions": ["resulting-PE", "durability-registry", "forced-power-loss-evidence"],
        "runtime_authority": "requires-task-7.2-native-producer-and-retained-source-loader",
    }
    if mode == "production":
        manifest["build_inputs"] = validate_build_inputs(repo, collector, hook, input_roots or {})
        if repo.read(GENERATOR_PATH) != generator_bytes:
            raise ManifestError("executed generator differs from tracked generator input")
    else:
        manifest["dirty_project_inputs"] = ordered(repo.dirty)
    repo.reprove()
    return GeneratedManifest(manifest, canonical(manifest), hook)


def validate_build_inputs(repo: Repository, collector: Collector, hook: bytes, input_roots: dict[str, Path]) -> dict[str, Any]:
    if BUILD_PLAN_PATH not in repo.tracked:
        raise ManifestError(f"missing production build inputs: {BUILD_PLAN_PATH}")
    plan_bytes = repo.read(BUILD_PLAN_PATH)
    plan = _object(_json(plan_bytes, BUILD_PLAN_PATH), {"schema_version", "build_sources", "input_trees", "native", "runtime_imports", "wheel_members", "resulting_pe"}, set(), "build inputs")
    if plan["schema_version"] != BUILD_SCHEMA:
        raise ManifestError("unknown production build input schema")
    resulting_pe = relative(plan["resulting_pe"])
    if not resulting_pe.lower().endswith(".exe"):
        raise ManifestError("resulting PE must name an EXE")
    sources = _object(plan["build_sources"], set(_MANDATORY_BUILD_FILES) | {GENERATOR_PATH}, set(plan["build_sources"]) if type(plan["build_sources"]) is dict else set(), "build sources")
    source_facts: dict[str, Any] = {}
    for name, expected in sources.items():
        relative(name)
        if name == resulting_pe:
            raise ManifestError("resulting PE cannot be a pre-link input")
        content = repo.read(name)
        _require_fact(content, expected, name)
        source_facts[name] = fact(content)
    # The whole versioned build-definition directory participates. Adding a
    # hook, lock, metadata or recipe without updating the plan must fail.
    packaging_files = {name for name in repo.tracked if name.startswith("packaging/windows/") and name != BUILD_PLAN_PATH and not name.startswith("packaging/windows/evidence-scenarios/")}
    actual_packaging = {"packaging/windows/" + name for name in _tree_bytes(repo.root / "packaging/windows") if not name.startswith("evidence-scenarios/")}
    untracked_definitions = actual_packaging - repo.tracked
    if untracked_definitions:
        raise ManifestError("untracked build definitions: " + ", ".join(ordered(untracked_definitions)))
    missing_sources = packaging_files - sources.keys() - {ROOTS_PATH}
    if missing_sources:
        raise ManifestError("unlisted build definitions: " + ", ".join(ordered(missing_sources)))
    # Discover actual project imports of build scripts, including transitive
    # tools/helpers; do not trust their role labels as completeness evidence.
    build_collector = Collector(repo)
    build_collector.external_roots.update(collector.external_roots)
    build_collector.external_roots.update({"PyInstaller", "pefile", "packaging", "altgraph", "win32ctypes"})
    for name in sources:
        if name.endswith((".py", ".spec")):
            build_collector.add(name, "build-definition", "build-source")
            if name.endswith(".py"):
                build_collector.declared.add(_module_name(name))
    build_collector.declared.update(collector.declared)
    build_collector.scan()
    missing_sources = set(build_collector.entries) - sources.keys() - collector.entries.keys()
    if missing_sources:
        raise ManifestError("unlisted transitive build sources: " + ", ".join(ordered(missing_sources)))
    trees = plan["input_trees"]
    if type(trees) is not dict or set(trees) != set(input_roots):
        raise ManifestError("missing or unknown isolated input root binding")
    tree_facts: dict[str, Any] = {}
    contents: dict[str, bytes] = {}
    kinds: dict[str, str] = {}
    locations: list[Path] = []
    for tree_id, declaration in trees.items():
        if re.fullmatch(r"[a-z][a-z0-9-]*", tree_id) is None:
            raise ManifestError("invalid input tree id")
        declaration = _object(declaration, {"kind", "files"}, set(), "input tree")
        if declaration["kind"] not in _TREE_KINDS:
            raise ManifestError(f"unknown input tree kind: {tree_id}")
        root = input_roots[tree_id].absolute()
        # External trees are immutable staging inputs, never an ambient venv or
        # a directory within the checkout (which would bypass Git tracking).
        resolved = root.resolve()
        if resolved == repo.root.resolve() or repo.root.resolve() in resolved.parents:
            raise ManifestError(f"untracked repository input tree: {tree_id}")
        if any(resolved == previous or resolved in previous.parents or previous in resolved.parents for previous in locations):
            raise ManifestError("overlapping input trees")
        locations.append(resolved)
        actual = _tree_bytes(root)
        expected_files = declaration["files"]
        if type(expected_files) is not dict or not expected_files or set(actual) != set(expected_files):
            raise ManifestError(f"input tree membership mismatch: {tree_id}")
        for name, content in actual.items():
            if name == resulting_pe or Path(name).name.casefold() == Path(resulting_pe).name.casefold():
                raise ManifestError("resulting PE cannot be a pre-link input")
            _require_fact(content, expected_files[name], f"{tree_id}/{name}")
            if _competing_code(name, set(collector.entries)):
                raise ManifestError(f"duplicate critical code in input tree: {tree_id}/{name}")
            contents[tree_id + "/" + name] = content
            kinds[tree_id + "/" + name] = declaration["kind"]
        tree_facts[tree_id] = {"kind": declaration["kind"], "files": {name: fact(actual[name]) for name in ordered(actual)}}
    if {entry["kind"] for entry in tree_facts.values()} != _TREE_KINDS:
        raise ManifestError("missing mandatory build input tree kind")
    critical_paths = {path for path, entry in collector.entries.items() if entry["kind"] == "critical-source"}
    for name, content in contents.items():
        if name.lower().endswith((".zip", ".whl")):
            _reject_critical_archive(content, name, critical_paths)
    wheels: dict[str, dict[str, bytes]] = {}
    for name, content in contents.items():
        if kinds[name] == "locked-wheels":
            if not name.endswith(".whl"):
                raise ManifestError(f"non-wheel in locked wheels input: {name}")
            wheels[name] = _wheel_members(content, name)
    relations = plan["wheel_members"]
    if type(relations) is not dict:
        raise ManifestError("wheel member provenance required")
    for target, origin in relations.items():
        origin = _object(origin, {"wheel", "member"}, set(), "wheel member provenance")
        if target not in contents or origin["wheel"] not in wheels or origin["member"] not in wheels[origin["wheel"]]:
            raise ManifestError(f"missing wheel member input: {target}")
        if contents[target] != wheels[origin["wheel"]][origin["member"]]:
            raise ManifestError(f"wheel/runtime byte mismatch: {target}")
    runtime_imports = plan["runtime_imports"]
    required_imports = collector.external_roots | {name.split(".")[0] for name in build_collector.external if name.split(".")[0] not in sys.stdlib_module_names}
    if type(runtime_imports) is not dict or set(runtime_imports) != required_imports:
        raise ManifestError("runtime import mappings differ from actual owner/build imports")
    for module, target in runtime_imports.items():
        if target not in contents or target not in relations:
            raise ManifestError(f"runtime import lacks locked wheel provenance: {module}")
        member = relations[target]["member"]
        module_path = module.replace(".", "/")
        if not (member == module_path + ".py" or member == module_path + "/__init__.py" or (member.startswith(module_path + ".") and member.endswith(".pyd"))):
            raise ManifestError(f"runtime import/file relation mismatch: {module}")
    native = _native_inputs(plan["native"], contents, kinds, collector.plugins)
    for entry in native["entries"].values():
        if _competing_code(entry["bundle_path"], set(collector.entries)):
            raise ManifestError(f"duplicate critical code at native bundle path: {entry['bundle_path']}")
    # Re-read every external tree after validation, not just listed members.
    for tree_id in trees:
        current = _tree_bytes(input_roots[tree_id])
        if {name: fact(content) for name, content in current.items()} != tree_facts[tree_id]["files"]:
            raise ManifestError(f"input tree drift: {tree_id}")
    return {"plan": {"path": BUILD_PLAN_PATH, **fact(plan_bytes)}, "repository_files": {name: source_facts[name] for name in ordered(source_facts)}, "input_trees": {name: tree_facts[name] for name in ordered(tree_facts)}, "native": native, "runtime_imports": runtime_imports, "wheel_members": relations, "generated_hook": {"recipe": GENERATOR_PATH, "owner_roots": ROOTS_PATH, **fact(hook)}, "resulting_pe_excluded": resulting_pe}


def validate_collection(manifest: GeneratedManifest, members: dict[str, bytes], *, pyz_modules: Iterable[str]) -> None:
    """Validate actual collected paths/bytes and the packager's PYZ TOC.

    The packager must obtain the TOC from its actual archive; this function is
    not a runtime loader proof and cannot attest a caller's imaginary TOC.
    """
    seen: set[str] = set()
    entries = {entry["path"]: entry for entry in manifest.manifest["entries"]}
    critical = {entry["module"]: path for path, entry in entries.items() if entry["kind"] == "critical-source"}
    for name, content in members.items():
        relative(name)
        if name.casefold() in seen:
            raise ManifestError(f"duplicate collected path: {name}")
        seen.add(name.casefold())
        if name in entries:
            _require_fact(content, {key: entries[name][key] for key in ("bytes", "sha256")}, name)
        if name not in entries and _competing_code(name, set(critical.values())):
            raise ManifestError(f"duplicate critical code: {name}")
        if name.lower().endswith((".zip", ".whl")):
            _reject_critical_archive(content, name, set(critical.values()))
    missing = entries.keys() - members.keys()
    if missing:
        raise ManifestError("missing collected inputs: " + ", ".join(ordered(missing)))
    duplicate = {name for name in pyz_modules if _competing_code(name.replace(".", "/") + ".py", set(critical.values()))}
    if duplicate:
        raise ManifestError("critical PYZ duplicate: " + ", ".join(ordered(duplicate)))


def _require_fact(content: bytes, expected: Any, label: str) -> None:
    _object(expected, {"bytes", "sha256"}, set(), "content address")
    if type(expected["bytes"]) is not int or expected["bytes"] < 0 or type(expected["sha256"]) is not str or re.fullmatch("[0-9a-f]{64}", expected["sha256"]) is None:
        raise ManifestError(f"invalid or missing content address: {label}")
    if fact(content) != expected:
        raise ManifestError(f"input digest/bytes mismatch: {label}")


def _reject_critical_archive(content: bytes, label: str, critical: set[str]) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                if _competing_code(name, critical):
                    raise ManifestError(f"duplicate critical archive member: {label}:{name}")
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ManifestError(f"invalid runtime input archive: {label}") from exc


def _code_identity(path: str) -> tuple[str, ...] | None:
    """Windows import identity, independent of host importer and ABI version.

    Tagged extension/bytecode stems and package __init__ members identify the
    same module. Extra prefixes cannot hide a redirected critical code copy.
    """
    parts = [part.casefold() for part in path.replace("\\", "/").split("/") if part.casefold() != "__pycache__"]
    if not parts or not parts[-1].endswith((".py", ".pyc", ".pyo", ".pyd")):
        return None
    parts[-1] = parts[-1].split(".", 1)[0]
    if parts[-1] == "__init__":
        parts.pop()
    return tuple(parts)


def _competing_code(path: str, critical: set[str]) -> bool:
    identity = _code_identity(path)
    if not identity:
        return False
    for source in critical:
        if not source.endswith(".py"):
            continue
        expected = _code_identity(source)
        if expected and len(identity) >= len(expected) and identity[-len(expected):] == expected:
            return True
    return False


def _tree_bytes(root: Path) -> dict[str, bytes]:
    if not root.is_dir() or root.is_symlink() or root.is_junction():
        raise ManifestError("input tree must be a real directory")
    # Reject reparses in ancestors too; resolve() alone would erase evidence.
    for parent in (root, *root.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ManifestError("reparse input tree ancestor")
    result: dict[str, bytes] = {}
    for directory, directories, files in root.walk(follow_symlinks=False):
        for item in (*directories, *files):
            child = directory / item
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ManifestError("reparse input tree member")
        for filename in files:
            name = relative((directory / filename).relative_to(root).as_posix())
            if name.casefold() in {item.casefold() for item in result}:
                raise ManifestError(f"duplicate Windows input path: {name}")
            result[name] = _read(root, name)
    return result


def _wheel_members(content: bytes, label: str) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            result: dict[str, bytes] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = relative(info.filename)
                if name.casefold() in {key.casefold() for key in result} or stat.S_ISLNK(info.external_attr >> 16):
                    raise ManifestError(f"duplicate/linked wheel member: {label}:{name}")
                result[name] = archive.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise ManifestError(f"invalid locked wheel: {label}") from exc
    records = [name for name in result if name.endswith(".dist-info/RECORD")]
    if len(records) != 1:
        raise ManifestError(f"missing/ambiguous wheel RECORD: {label}")
    rows = list(csv.reader(io.StringIO(result[records[0]].decode("utf-8"))))
    seen: set[str] = set()
    for row in rows:
        if len(row) != 3 or row[0] in seen or row[0] not in result:
            raise ManifestError(f"invalid wheel RECORD: {label}")
        name, digest, size = row
        seen.add(name)
        if name == records[0]:
            if digest or size:
                raise ManifestError("wheel RECORD self-reference")
            continue
        expected = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(result[name]).digest()).decode().rstrip("=")
        if digest != expected or size != str(len(result[name])):
            raise ManifestError(f"wheel RECORD mismatch: {label}:{name}")
    if seen != set(result):
        raise ManifestError(f"unlisted wheel member: {label}")
    return result


def _native_inputs(value: Any, contents: dict[str, bytes], kinds: dict[str, str], plugins: set[str]) -> dict[str, Any]:
    value = _object(value, {"entries", "dynamic_roots", "python_dll", "bootloader"}, set(), "native inputs")
    entries = value["entries"]
    if type(entries) is not dict or not entries:
        raise ManifestError("native entries required")
    native_files = {name for name in contents if name.lower().endswith((".dll", ".pyd")) or kinds[name] == "bootloader" and name.lower().endswith(".exe")}
    if set(entries) != native_files:
        raise ManifestError("native inventory differs from actual isolated trees")
    bundle_paths: set[str] = set()
    for source, entry in entries.items():
        _object(entry, {"bundle_path", "dependencies"}, set(), "native entry")
        path = relative(entry["bundle_path"])
        if path.casefold() in {item.casefold() for item in bundle_paths}:
            raise ManifestError(f"duplicate native bundle path: {path}")
        bundle_paths.add(path)
        if not contents[source].startswith(b"MZ"):
            raise ManifestError(f"native input lacks PE header: {source}")
        dependencies = _strings(entry["dependencies"], "native dependencies")
        if not set(dependencies) <= entries.keys():
            raise ManifestError(f"missing native dependency: {source}")
    if value["python_dll"] not in entries or kinds[value["python_dll"]] != "python-runtime":
        raise ManifestError("Python DLL input missing")
    if value["bootloader"] not in entries or kinds[value["bootloader"]] != "bootloader":
        raise ManifestError("bootloader input missing")
    roots = _strings(value["dynamic_roots"], "dynamic native roots", nonempty=True)
    if not set(roots) <= entries.keys():
        raise ManifestError("undeclared dynamic native root")
    for plugin in plugins:
        if not any(entries[name]["bundle_path"].endswith("/" + plugin) or entries[name]["bundle_path"] == plugin for name in roots):
            raise ManifestError(f"missing dynamic Qt plugin root: {plugin}")
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(name: str) -> None:
        if name in visiting:
            raise ManifestError(f"native dependency cycle: {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in entries[name]["dependencies"]:
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    for name in entries:
        visit(name)
    # This is a content-addressed producer input graph, not an assertion that
    # the operating system will load it. Task 7.2 consumes and re-proves it.
    return {"graph_kind": "native-input-dag-not-runtime-attestation", "entries": {name: {**entries[name], **fact(contents[name])} for name in ordered(entries)}, "dynamic_roots": ordered(roots), "python_dll": value["python_dll"], "bootloader": value["bootloader"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("production", "development"), default="production")
    parser.add_argument("--input-root", action="append", default=[], metavar="ID=PATH")
    args = parser.parse_args(argv)
    try:
        roots: dict[str, Path] = {}
        for raw in args.input_root:
            name, separator, location = raw.partition("=")
            if not separator or not name or name in roots:
                raise ManifestError("invalid or duplicate input root binding")
            roots[name] = Path(location)
        result = generate(args.repository, mode=args.mode, input_roots=roots)
        args.output.mkdir(parents=True, exist_ok=False)
        (args.output / "hooks").mkdir()
        (args.output / "frozen-source-manifest.json").write_bytes(result.manifest_bytes)
        (args.output / "hooks/hook-capability_host.py").write_bytes(result.hook_bytes)
        print(json.dumps({"classification": result.manifest["classification"], "repository_commit": result.manifest["repository_commit"], "entries": len(result.manifest["entries"]), "manifest": fact(result.manifest_bytes), "hook": fact(result.hook_bytes)}, sort_keys=True))
        return 0
    except (ManifestError, OSError) as exc:
        print(f"frozen manifest rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
