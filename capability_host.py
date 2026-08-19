"""Qt-free host for the Matcher Gate and Gate C/D TM retrieval state.

The host starts exact-only, then lets the application composition owner rerun
the Core validated matcher factory for the loaded checkout.  Ordinary callers
only receive immutable snapshots and generation notifications.  Gate C uses
the paired release recomputed for this checkout to replace the complete
retrieval service; Gate D may refresh only that same formal graph.
No capability is inferred from booleans, store health, or display state.
"""

from __future__ import annotations

import ast
from dataclasses import (
    MISSING,
    dataclass,
    field as dataclass_field,
    fields as dataclass_fields,
    is_dataclass,
    make_dataclass,
)
from datetime import datetime, timezone
from enum import Enum
import hashlib
import importlib
from importlib.machinery import ModuleSpec, SourceFileLoader
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from threading import Condition, Lock, RLock, Thread
from types import CodeType, FunctionType, ModuleType
from typing import (
    Any,
    Callable,
    Protocol,
    TypeVar,
    cast,
    final,
    runtime_checkable,
)

from capability_gated_text_matcher import CapabilityGatedTextMatcherV1
from editor_contracts import RetrievalDisplayState, TextMatcherDisplayState
from matcher_validation import build_validated_matcher_v1
from tm_contracts import (
    CapabilityGatedTextMatcher,
    QueryReport,
    TMQuery,
    TMResourceHandle,
    TextMatcherCapability,
    TextMatcherState,
)
from tm_retrieval import TMRetrievalService
from tm_retrieval_capability import (
    RetrievalCapabilityEvaluator,
    RetrievalCapabilityExpectation,
    RetrievalCapabilityManifest,
    RetrievalCapabilityPublisher,
    RetrievalCapabilitySnapshot,
    default_retrieval_capability_publisher,
)


_OperationResultT = TypeVar("_OperationResultT")
_MATCHER_CLOSED_REASON = "MATCHER.VALIDATION_UNAVAILABLE"
_COMPOSITION_MINT_IDENTITY = object()
_RETRIEVAL_VALIDATION_MODULE_NAME = "tm_retrieval_" + "validation"
_RETRIEVAL_VALIDATION_FUNCTION_NAME = "recompute_retrieval_validation"
_RETRIEVAL_APPROVED_ROOTS_RELATIVE_PATH = (
    Path("tests") / "fixtures" / "retrieval_gate_c_roots_v1.json"
)
_GATE_D_CONTRACT_RELATIVE_PATH = Path("benchmark_tm_contract.json")
_GATE_D_MODULE_NAMES = (
    "tm_benchmark",
    "tm_benchmark_latency",
    "tm_benchmark_oracle",
    "tm_benchmark_process",
    "tm_benchmark_query_process",
    "tm_retrieval_capability",
    "tm_benchmark_gate",
)


@final
class _RetrievalGenerationChanged(RuntimeError):
    """Application-private signal for a stale retrieval commit reservation."""


@final
class _MatcherGenerationChanged(RuntimeError):
    """Application-private signal for a stale matcher commit reservation."""


def _require_generation(value: object) -> None:
    if type(value) is not int:
        raise TypeError("capability generation must be an exact integer")
    if value < 0:
        raise ValueError("capability generation must be non-negative")


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    path: Path
    device: int
    inode: int
    directory: bool

    @classmethod
    def capture(cls, path: Path, *, directory: bool) -> _PathIdentity:
        resolved = path.resolve(strict=True)
        path_stat = resolved.lstat()
        expected_kind = (
            stat.S_ISDIR(path_stat.st_mode)
            if directory
            else stat.S_ISREG(path_stat.st_mode)
        )
        if not expected_kind:
            kind = "directory" if directory else "regular file"
            raise RuntimeError(f"application checkout identity requires {kind}")
        return cls(
            path=resolved,
            device=path_stat.st_dev,
            inode=path_stat.st_ino,
            directory=directory,
        )

    def is_current(self) -> bool:
        try:
            if self.path.resolve(strict=True) != self.path:
                return False
            path_stat = self.path.lstat()
        except OSError:
            return False
        expected_kind = (
            stat.S_ISDIR(path_stat.st_mode)
            if self.directory
            else stat.S_ISREG(path_stat.st_mode)
        )
        return (
            expected_kind
            and path_stat.st_dev == self.device
            and path_stat.st_ino == self.inode
        )


def _read_regular_file_no_follow(
    path: Path,
) -> tuple[int, int, bytes]:
    """Read one exact regular file without following its final path entry."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError("trusted checkout anchor must be a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        raise RuntimeError("trusted checkout anchor changed while reading")
    return before.st_dev, before.st_ino, b"".join(chunks)


@dataclass(frozen=True, slots=True)
class _TrackedFileAnchor:
    """Import-time bytes and file identity for a fixed checkout artifact."""

    path: Path
    device: int
    inode: int
    digest: str
    content: bytes

    @classmethod
    def capture(cls, path: Path) -> _TrackedFileAnchor:
        absolute = path.absolute()
        if absolute.resolve(strict=True) != absolute:
            raise RuntimeError("trusted checkout anchor must not be a symlink")
        device, inode, content = _read_regular_file_no_follow(absolute)
        return cls(
            path=absolute,
            device=device,
            inode=inode,
            digest=hashlib.sha256(content).hexdigest(),
            content=content,
        )

    def is_current(self) -> bool:
        try:
            if self.path.resolve(strict=True) != self.path:
                return False
            device, inode, content = _read_regular_file_no_follow(self.path)
        except (OSError, RuntimeError):
            return False
        return (
            device == self.device
            and inode == self.inode
            and hashlib.sha256(content).hexdigest() == self.digest
            and content == self.content
        )


@dataclass(frozen=True, slots=True)
class _ApplicationCheckoutIdentity:
    root: _PathIdentity
    host_module: _PathIdentity
    matcher_factory_source: _PathIdentity

    @classmethod
    def capture(
        cls,
        factory_source: _PathIdentity,
        approved_roots: _PathIdentity,
    ) -> _ApplicationCheckoutIdentity:
        host_path = Path(__file__).resolve(strict=True)
        root_path = host_path.parent
        if factory_source.path.parent != root_path:
            raise RuntimeError(
                "matcher factory must be loaded from the application checkout"
            )
        try:
            approved_roots.path.relative_to(root_path)
        except ValueError:
            raise RuntimeError(
                "matcher approved roots must belong to the application checkout"
            ) from None
        return cls(
            root=_PathIdentity.capture(root_path, directory=True),
            host_module=_PathIdentity.capture(host_path, directory=False),
            matcher_factory_source=factory_source,
        )

    def is_current(self) -> bool:
        if (
            self.host_module.path.parent != self.root.path
            or self.matcher_factory_source.path.parent != self.root.path
        ):
            return False
        return (
            self.root.is_current()
            and self.host_module.is_current()
            and self.matcher_factory_source.is_current()
        )


def _function_defaults_snapshot(
    values: tuple[object, ...] | None,
) -> tuple[tuple[str, str], ...] | None:
    if values is None:
        return None
    return tuple((type(value).__qualname__, repr(value)) for value in values)


def _function_kwdefaults_snapshot(
    values: dict[str, object] | None,
) -> tuple[tuple[str, str, str], ...] | None:
    if values is None:
        return None
    return tuple(
        sorted(
            (key, type(value).__qualname__, repr(value))
            for key, value in values.items()
        )
    )


def _function_closure_snapshot(
    values: tuple[object, ...] | None,
) -> tuple[tuple[int, int, str, str], ...] | None:
    if values is None:
        return None
    observed: list[tuple[int, int, str, str]] = []
    for raw_cell in values:
        cell = raw_cell
        try:
            contents = cast(Any, cell).cell_contents
        except ValueError:
            observed.append((id(cell), 0, "EMPTY", ""))
        else:
            observed.append(
                (
                    id(cell),
                    id(contents),
                    type(contents).__qualname__,
                    repr(contents),
                )
            )
    return tuple(observed)


def _code_constant_fingerprint(value: object) -> object:
    if type(value) is CodeType:
        return ("CODE", _code_fingerprint(cast(CodeType, value)))
    if type(value) is tuple:
        return (
            "TUPLE",
            tuple(
                _code_constant_fingerprint(item)
                for item in cast(tuple[object, ...], value)
            ),
        )
    if type(value) is frozenset:
        return (
            "FROZENSET",
            tuple(
                sorted(
                    repr(_code_constant_fingerprint(item))
                    for item in cast(frozenset[object], value)
                )
            ),
        )
    return (type(value).__module__, type(value).__qualname__, value)


def _code_fingerprint(code: CodeType) -> tuple[object, ...]:
    """Return an exact structural fingerprint independent of object identity."""

    return (
        code.co_argcount,
        code.co_posonlyargcount,
        code.co_kwonlyargcount,
        code.co_nlocals,
        code.co_stacksize,
        code.co_flags,
        code.co_code,
        tuple(
            _code_constant_fingerprint(value) for value in code.co_consts
        ),
        code.co_names,
        code.co_varnames,
        code.co_filename,
        code.co_name,
        code.co_qualname,
        code.co_firstlineno,
        code.co_linetable,
        code.co_exceptiontable,
        code.co_freevars,
        code.co_cellvars,
    )


@dataclass(frozen=True, slots=True)
class _SourceFunctionDefaults:
    positional: tuple[object, ...]
    keywords: tuple[tuple[str, object], ...]


@dataclass(frozen=True, slots=True)
class _DataclassFieldShape:
    name: str
    has_default: bool
    init: bool
    repr: bool
    compare: bool
    hash_value: bool | None
    kw_only: bool


@dataclass(frozen=True, slots=True)
class _GeneratedCallableShape:
    code: tuple[object, ...]
    positional_defaults: tuple[object, ...] | None
    keyword_defaults: tuple[tuple[str, object], ...] | None
    closure_values: tuple[object, ...] | None


@dataclass(frozen=True, slots=True)
class _DataclassSourceShape:
    qualname: str
    init: bool
    repr: bool
    eq: bool
    order: bool
    unsafe_hash: bool
    frozen: bool
    match_args: bool
    kw_only: bool
    slots: bool
    weakref_slot: bool
    fields: tuple[_DataclassFieldShape, ...]
    generated_codes: tuple[
        tuple[str, tuple[tuple[object, ...], ...]], ...
    ]
    generated_callables: tuple[
        tuple[str, tuple[_GeneratedCallableShape, ...]], ...
    ]
    prototype_class: type[object]

    def expected_codes(
        self,
        member_name: str,
    ) -> tuple[tuple[object, ...], ...] | None:
        return dict(self.generated_codes).get(member_name)

    def expected_callables(
        self,
        member_name: str,
    ) -> tuple[_GeneratedCallableShape, ...] | None:
        return dict(self.generated_callables).get(member_name)


def _literal_bool_keyword(
    keywords: list[ast.keyword],
    name: str,
    default: bool,
) -> bool:
    matches = tuple(keyword for keyword in keywords if keyword.arg == name)
    if not matches:
        return default
    if len(matches) != 1:
        raise RuntimeError("dataclass option must not repeat")
    value = ast.literal_eval(matches[0].value)
    if type(value) is not bool:
        raise RuntimeError("dataclass option must be a boolean literal")
    return cast(bool, value)


def _dataclass_decorator_options(
    node: ast.ClassDef,
) -> dict[str, bool] | None:
    matches: list[ast.expr] = []
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == "dataclass":
            matches.append(decorator)
        elif (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "dataclass"
        ):
            matches.append(decorator)
    if not matches:
        return None
    if len(matches) != 1:
        raise RuntimeError("dataclass decorator must not repeat")
    decorator = matches[0]
    keywords = decorator.keywords if isinstance(decorator, ast.Call) else []
    if isinstance(decorator, ast.Call) and decorator.args:
        raise RuntimeError("dataclass decorator positional args are unsupported")
    return {
        "init": _literal_bool_keyword(keywords, "init", True),
        "repr": _literal_bool_keyword(keywords, "repr", True),
        "eq": _literal_bool_keyword(keywords, "eq", True),
        "order": _literal_bool_keyword(keywords, "order", False),
        "unsafe_hash": _literal_bool_keyword(
            keywords,
            "unsafe_hash",
            False,
        ),
        "frozen": _literal_bool_keyword(keywords, "frozen", False),
        "match_args": _literal_bool_keyword(keywords, "match_args", True),
        "kw_only": _literal_bool_keyword(keywords, "kw_only", False),
        "slots": _literal_bool_keyword(keywords, "slots", False),
        "weakref_slot": _literal_bool_keyword(
            keywords,
            "weakref_slot",
            False,
        ),
    }


def _dataclass_field_shape(node: ast.AnnAssign) -> _DataclassFieldShape:
    if not isinstance(node.target, ast.Name):
        raise RuntimeError("dataclass field name must be static")
    options = {
        "init": True,
        "repr": True,
        "compare": True,
        "kw_only": False,
    }
    hash_value: bool | None = None
    has_default = node.value is not None
    if (
        isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "field"
    ):
        if node.value.args:
            raise RuntimeError("dataclass field positional args are unsupported")
        options = {
            key: _literal_bool_keyword(node.value.keywords, key, default)
            for key, default in options.items()
        }
        hash_keywords = tuple(
            keyword
            for keyword in node.value.keywords
            if keyword.arg == "hash"
        )
        if hash_keywords:
            if len(hash_keywords) != 1:
                raise RuntimeError("dataclass field hash must not repeat")
            raw_hash = ast.literal_eval(hash_keywords[0].value)
            if raw_hash is not None and type(raw_hash) is not bool:
                raise RuntimeError(
                    "dataclass field hash must be bool or None"
                )
            hash_value = cast(bool | None, raw_hash)
        has_default = any(
            keyword.arg in {"default", "default_factory"}
            for keyword in node.value.keywords
        )
    return _DataclassFieldShape(
        name=node.target.id,
        has_default=has_default,
        init=options["init"],
        repr=options["repr"],
        compare=options["compare"],
        hash_value=hash_value,
        kw_only=options["kw_only"],
    )


def _dataclass_source_shape(
    node: ast.ClassDef,
    qualname: str,
    options: dict[str, bool],
) -> _DataclassSourceShape:
    field_shapes = tuple(
        _dataclass_field_shape(item)
        for item in node.body
        if isinstance(item, ast.AnnAssign)
    )
    prototype_fields: list[tuple[str, type[object], object]] = []
    for shape in field_shapes:
        if shape.has_default:
            prototype_field = dataclass_field(
                default=None,
                init=shape.init,
                repr=shape.repr,
                compare=shape.compare,
                hash=shape.hash_value,
                kw_only=shape.kw_only,
            )
        else:
            prototype_field = dataclass_field(
                init=shape.init,
                repr=shape.repr,
                compare=shape.compare,
                hash=shape.hash_value,
                kw_only=shape.kw_only,
            )
        prototype_fields.append(
            (
                shape.name,
                object,
                prototype_field,
            )
        )

    explicit_methods = {
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def prototype_post_init(_: object) -> None:
        return None

    namespace: dict[str, object] = {}
    if "__post_init__" in explicit_methods:
        namespace["__post_init__"] = prototype_post_init
    prototype = make_dataclass(
        node.name,
        prototype_fields,
        namespace=namespace,
        init=options["init"],
        repr=options["repr"],
        eq=options["eq"],
        order=options["order"],
        unsafe_hash=options["unsafe_hash"],
        frozen=options["frozen"],
        match_args=options["match_args"],
        kw_only=options["kw_only"],
        slots=options["slots"],
        weakref_slot=options["weakref_slot"],
    )
    generated_codes: list[
        tuple[str, tuple[tuple[object, ...], ...]]
    ] = []
    generated_callables: list[
        tuple[str, tuple[_GeneratedCallableShape, ...]]
    ] = []
    for member_name, member in vars(prototype).items():
        if (
            member_name in explicit_methods
            or member_name == "__annotate_func__"
        ):
            continue
        functions = _static_functions(member)
        if functions:
            generated_codes.append(
                (
                    member_name,
                    tuple(
                        _code_fingerprint(function.__code__)
                        for function in functions
                    ),
                )
            )
            generated_callables.append(
                (
                    member_name,
                    tuple(
                        _GeneratedCallableShape(
                            code=_code_fingerprint(function.__code__),
                            positional_defaults=function.__defaults__,
                            keyword_defaults=(
                                None
                                if function.__kwdefaults__ is None
                                else tuple(
                                    sorted(
                                        function.__kwdefaults__.items()
                                    )
                                )
                            ),
                            closure_values=(
                                None
                                if function.__closure__ is None
                                else tuple(
                                    cell.cell_contents
                                    for cell in function.__closure__
                                )
                            ),
                        )
                        for function in functions
                    ),
                )
            )
    return _DataclassSourceShape(
        qualname=qualname,
        init=options["init"],
        repr=options["repr"],
        eq=options["eq"],
        order=options["order"],
        unsafe_hash=options["unsafe_hash"],
        frozen=options["frozen"],
        match_args=options["match_args"],
        kw_only=options["kw_only"],
        slots=options["slots"],
        weakref_slot=options["weakref_slot"],
        fields=field_shapes,
        generated_codes=tuple(sorted(generated_codes)),
        generated_callables=tuple(sorted(generated_callables)),
        prototype_class=prototype,
    )


def _source_default_value(
    node: ast.expr,
    named_defaults: dict[str, object],
) -> object:
    if isinstance(node, ast.Name) and node.id in named_defaults:
        return named_defaults[node.id]
    if isinstance(node, ast.Attribute):
        qualified = ast.unparse(node)
        if qualified in named_defaults:
            return named_defaults[qualified]
    try:
        return ast.literal_eval(node)
    except (TypeError, ValueError):
        raise RuntimeError("trusted source uses an unsupported default") from None


def _source_declarations(
    content: bytes,
    *,
    named_defaults: dict[str, object],
) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, _SourceFunctionDefaults], ...],
    tuple[tuple[str, str, str], ...],
    tuple[_DataclassSourceShape, ...],
]:
    tree = ast.parse(content)
    resolved_defaults = dict(named_defaults)
    for node in tree.body:
        name: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            name = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(
            node.target,
            ast.Name,
        ):
            name = node.target.id
            value = node.value
        if name is None or value is None or name in resolved_defaults:
            continue
        try:
            resolved_defaults[name] = ast.literal_eval(value)
        except (TypeError, ValueError):
            pass
    functions: list[str] = []
    classes: list[str] = []
    defaults: dict[str, _SourceFunctionDefaults] = {}
    core_imports: list[tuple[str, str, str]] = []
    dataclass_shapes: list[_DataclassSourceShape] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (
            node.module is not None and node.module.startswith("tm_")
        ):
            core_imports.extend(
                (
                    alias.asname or alias.name,
                    node.module,
                    alias.name,
                )
                for alias in node.names
                if alias.name != "*"
            )
        elif isinstance(node, ast.Import):
            core_imports.extend(
                (
                    alias.asname or alias.name,
                    alias.name,
                    "",
                )
                for alias in node.names
                if alias.name.startswith("tm_")
            )

    def visit_body(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                functions.append(qualname)
                positional = tuple(
                    _source_default_value(value, resolved_defaults)
                    for value in node.args.defaults
                )
                keywords = tuple(
                    (
                        argument.arg,
                        _source_default_value(value, resolved_defaults),
                    )
                    for argument, value in zip(
                        node.args.kwonlyargs,
                        node.args.kw_defaults,
                        strict=True,
                    )
                    if value is not None
                )
                defaults[qualname] = _SourceFunctionDefaults(
                    positional=positional,
                    keywords=keywords,
                )
            elif isinstance(node, ast.ClassDef):
                qualname = f"{prefix}.{node.name}" if prefix else node.name
                classes.append(qualname)
                dataclass_options = _dataclass_decorator_options(node)
                if dataclass_options is not None:
                    dataclass_shapes.append(
                        _dataclass_source_shape(
                            node,
                            qualname,
                            dataclass_options,
                        )
                    )
                visit_body(node.body, qualname)

    visit_body(tree.body, "")
    return (
        tuple(sorted(set(functions))),
        tuple(sorted(set(classes))),
        tuple(sorted(defaults.items(), key=lambda item: item[0])),
        tuple(sorted(set(core_imports))),
        tuple(sorted(dataclass_shapes, key=lambda shape: shape.qualname)),
    )


def _compiled_code_anchors(
    content: bytes,
    path: Path,
    declared_functions: tuple[str, ...],
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    accepted = set(declared_functions)
    observed: dict[str, list[tuple[object, ...]]] = {
        qualname: [] for qualname in declared_functions
    }

    def visit(code: CodeType) -> None:
        for value in code.co_consts:
            if type(value) is not CodeType:
                continue
            nested = cast(CodeType, value)
            if nested.co_qualname in accepted:
                observed[nested.co_qualname].append(
                    _code_fingerprint(nested)
                )
            visit(nested)

    visit(compile(content, str(path), "exec"))
    if any(not values for values in observed.values()):
        raise RuntimeError("trusted source callable code is incomplete")
    return tuple(
        (
            qualname,
            tuple(values),
        )
        for qualname, values in sorted(observed.items())
    )


@dataclass(frozen=True, slots=True)
class _ModuleSourceCodeAnchor:
    """Canonical source identity plus every declared runtime callable code."""

    module_name: str
    source: _TrackedFileAnchor
    function_codes: tuple[
        tuple[str, tuple[tuple[object, ...], ...]], ...
    ]
    function_defaults: tuple[tuple[str, _SourceFunctionDefaults], ...]
    class_qualnames: tuple[str, ...]
    core_imports: tuple[tuple[str, str, str], ...]
    dataclass_shapes: tuple[_DataclassSourceShape, ...]

    @classmethod
    def capture(
        cls,
        *,
        module_name: str,
        path: Path,
        named_defaults: dict[str, object] | None = None,
    ) -> _ModuleSourceCodeAnchor:
        source = _TrackedFileAnchor.capture(path)
        functions, classes, defaults, core_imports, dataclass_shapes = (
            _source_declarations(
                source.content,
                named_defaults=(
                    {} if named_defaults is None else named_defaults
                ),
            )
        )
        return cls(
            module_name=module_name,
            source=source,
            function_codes=_compiled_code_anchors(
                source.content,
                source.path,
                functions,
            ),
            function_defaults=defaults,
            class_qualnames=classes,
            core_imports=core_imports,
            dataclass_shapes=dataclass_shapes,
        )

    def expected_codes(
        self,
        qualname: str,
    ) -> tuple[tuple[object, ...], ...] | None:
        return dict(self.function_codes).get(qualname)

    def expected_defaults(
        self,
        qualname: str,
    ) -> _SourceFunctionDefaults | None:
        return dict(self.function_defaults).get(qualname)

    def dataclass_shape(
        self,
        qualname: str,
    ) -> _DataclassSourceShape | None:
        return {
            shape.qualname: shape for shape in self.dataclass_shapes
        }.get(qualname)

    def matches_module(self, module: ModuleType) -> bool:
        spec = module.__spec__
        loader = module.__loader__
        if type(spec) is not ModuleSpec or type(loader) is not SourceFileLoader:
            return False
        try:
            module_path = Path(cast(str, module.__file__)).resolve(strict=True)
            spec_origin = Path(cast(str, spec.origin)).resolve(strict=True)
            loader_path = Path(
                cast(str, loader.path)
            ).resolve(strict=True)
        except (AttributeError, OSError, TypeError, ValueError):
            return False
        return (
            self.source.is_current()
            and module.__name__ == self.module_name
            and module.__package__ == ""
            and sys.modules.get(self.module_name) is module
            and spec.name == self.module_name
            and spec.loader is loader
            and loader.name == self.module_name
            and module_path == self.source.path
            and spec_origin == self.source.path
            and loader_path == self.source.path
        )


def _resolve_static_object(module: ModuleType, qualname: str) -> object | None:
    current: object = module
    for part in qualname.split("."):
        if type(current) is ModuleType:
            current = cast(ModuleType, current).__dict__.get(part)
        elif isinstance(current, type):
            key = part
            if part.startswith("__") and not part.endswith("__"):
                class_name = current.__name__.lstrip("_")
                key = f"_{class_name}{part}"
            current = vars(current).get(key)
        else:
            return None
        if current is None:
            return None
    return current


def _static_functions(value: object) -> tuple[FunctionType, ...]:
    if type(value) is FunctionType:
        function = cast(FunctionType, value)
        while type(getattr(function, "__wrapped__", None)) is FunctionType:
            function = cast(FunctionType, function.__wrapped__)
        return (function,)
    if isinstance(value, (staticmethod, classmethod)):
        function = value.__func__
        return (function,) if type(function) is FunctionType else ()
    if isinstance(value, property):
        return tuple(
            cast(FunctionType, function)
            for function in (value.fget, value.fset, value.fdel)
            if type(function) is FunctionType
        )
    return ()


def _exact_defaults_match(
    function: FunctionType,
    expected: _SourceFunctionDefaults,
) -> bool:
    positional = function.__defaults__ or ()
    keywords = function.__kwdefaults__ or {}
    return (
        len(positional) == len(expected.positional)
        and all(
            type(actual) is type(wanted) and actual == wanted
            for actual, wanted in zip(
                positional,
                expected.positional,
                strict=True,
            )
        )
        and set(keywords) == {name for name, _ in expected.keywords}
        and all(
            type(keywords[name]) is type(wanted)
            and keywords[name] == wanted
            for name, wanted in expected.keywords
        )
    )


@dataclass(frozen=True, slots=True)
class _RuntimeFunctionIdentity:
    qualname: str
    function: FunctionType
    defaults_identity: tuple[object, ...] | None
    kwdefaults_identity: dict[str, object] | None
    closure_identity: tuple[object, ...] | None
    closure_snapshot: tuple[tuple[int, int, str, str], ...] | None

    @classmethod
    def capture(
        cls,
        qualname: str,
        function: FunctionType,
    ) -> _RuntimeFunctionIdentity:
        closure = cast(tuple[object, ...] | None, function.__closure__)
        return cls(
            qualname=qualname,
            function=function,
            defaults_identity=function.__defaults__,
            kwdefaults_identity=function.__kwdefaults__,
            closure_identity=closure,
            closure_snapshot=_function_closure_snapshot(closure),
        )

    def is_current(
        self,
        module: ModuleType,
        anchor: _ModuleSourceCodeAnchor,
    ) -> bool:
        expected_codes = anchor.expected_codes(self.qualname)
        expected_defaults = anchor.expected_defaults(self.qualname)
        function = self.function
        try:
            code_path = Path(function.__code__.co_filename).resolve(strict=True)
        except (OSError, TypeError, ValueError):
            return False
        return (
            expected_codes is not None
            and expected_defaults is not None
            and type(function) is FunctionType
            and function.__module__ == anchor.module_name
            and function.__qualname__ == self.qualname
            and function.__globals__ is module.__dict__
            and _code_fingerprint(function.__code__) in expected_codes
            and code_path == anchor.source.path
            and function.__defaults__ is self.defaults_identity
            and function.__kwdefaults__ is self.kwdefaults_identity
            and _exact_defaults_match(function, expected_defaults)
            and function.__closure__ is self.closure_identity
            and _function_closure_snapshot(
                cast(tuple[object, ...] | None, function.__closure__)
            )
            == self.closure_snapshot
        )


@dataclass(frozen=True, slots=True)
class _RuntimeCallableState:
    function: FunctionType
    code_identity: CodeType
    defaults_identity: tuple[object, ...] | None
    defaults_snapshot: tuple[tuple[str, str], ...] | None
    kwdefaults_identity: dict[str, object] | None
    kwdefaults_snapshot: tuple[tuple[str, str, str], ...] | None
    closure_identity: tuple[object, ...] | None
    closure_snapshot: tuple[tuple[int, int, str, str], ...] | None

    @classmethod
    def capture(
        cls,
        function: FunctionType,
    ) -> _RuntimeCallableState:
        closure = cast(tuple[object, ...] | None, function.__closure__)
        return cls(
            function=function,
            code_identity=function.__code__,
            defaults_identity=function.__defaults__,
            defaults_snapshot=_function_defaults_snapshot(
                function.__defaults__
            ),
            kwdefaults_identity=function.__kwdefaults__,
            kwdefaults_snapshot=_function_kwdefaults_snapshot(
                function.__kwdefaults__
            ),
            closure_identity=closure,
            closure_snapshot=_function_closure_snapshot(closure),
        )

    def is_current(self) -> bool:
        function = self.function
        return (
            type(function) is FunctionType
            and function.__code__ is self.code_identity
            and function.__defaults__ is self.defaults_identity
            and _function_defaults_snapshot(function.__defaults__)
            == self.defaults_snapshot
            and function.__kwdefaults__ is self.kwdefaults_identity
            and _function_kwdefaults_snapshot(function.__kwdefaults__)
            == self.kwdefaults_snapshot
            and function.__closure__ is self.closure_identity
            and _function_closure_snapshot(
                cast(tuple[object, ...] | None, function.__closure__)
            )
            == self.closure_snapshot
        )


def _effective_class_members(
    value: type[object],
) -> tuple[tuple[str, object], ...]:
    observed: dict[str, object] = {}
    for base in value.__mro__:
        for name, member in vars(base).items():
            observed.setdefault(name, member)
    return tuple(sorted(observed.items(), key=lambda item: item[0]))


def _member_identities_match(
    current: tuple[tuple[str, object], ...],
    captured: tuple[tuple[str, object], ...],
) -> bool:
    """Compare a class member graph without invoking member equality."""

    return (
        len(current) == len(captured)
        and all(
            current_name == captured_name
            and current_value is captured_value
            for (
                current_name,
                current_value,
            ), (
                captured_name,
                captured_value,
            ) in zip(current, captured, strict=True)
        )
    )


def _generated_value_matches(
    actual: object,
    expected: object,
    *,
    prototype_class: type[object],
    runtime_class: type[object],
) -> bool:
    if expected is prototype_class:
        return actual is runtime_class
    if type(expected) in (type(None), bool, int, float, str, bytes):
        return type(actual) is type(expected) and actual == expected
    if type(expected) is tuple and type(actual) is tuple:
        expected_tuple = cast(tuple[object, ...], expected)
        actual_tuple = cast(tuple[object, ...], actual)
        return len(actual_tuple) == len(expected_tuple) and all(
            _generated_value_matches(
                actual_item,
                expected_item,
                prototype_class=prototype_class,
                runtime_class=runtime_class,
            )
            for actual_item, expected_item in zip(
                actual_tuple,
                expected_tuple,
                strict=True,
            )
        )
    return actual is expected


def _generated_callable_matches(
    function: FunctionType,
    expected: _GeneratedCallableShape,
    *,
    prototype_class: type[object],
    runtime_class: type[object],
    compare_defaults: bool,
) -> bool:
    closure_values = (
        None
        if function.__closure__ is None
        else tuple(cell.cell_contents for cell in function.__closure__)
    )
    if expected.closure_values is None:
        closure_matches = closure_values is None
    elif closure_values is None:
        closure_matches = False
    else:
        closure_matches = _generated_value_matches(
            closure_values,
            expected.closure_values,
            prototype_class=prototype_class,
            runtime_class=runtime_class,
        )
    defaults_match = True
    if compare_defaults:
        defaults_match = _generated_value_matches(
            function.__defaults__,
            expected.positional_defaults,
            prototype_class=prototype_class,
            runtime_class=runtime_class,
        )
        keyword_defaults = (
            None
            if function.__kwdefaults__ is None
            else tuple(sorted(function.__kwdefaults__.items()))
        )
        defaults_match = defaults_match and _generated_value_matches(
            keyword_defaults,
            expected.keyword_defaults,
            prototype_class=prototype_class,
            runtime_class=runtime_class,
        )
    return (
        _code_fingerprint(function.__code__) == expected.code
        and defaults_match
        and closure_matches
    )


def _dataclass_shape_is_current(
    value: type[object],
    shape: _DataclassSourceShape,
) -> bool:
    if not is_dataclass(value):
        return False
    parameters = getattr(value, "__dataclass_params__", None)
    if parameters is None:
        return False
    expected_parameters = {
        "init": shape.init,
        "repr": shape.repr,
        "eq": shape.eq,
        "order": shape.order,
        "unsafe_hash": shape.unsafe_hash,
        "frozen": shape.frozen,
        "match_args": shape.match_args,
        "kw_only": shape.kw_only,
        "slots": shape.slots,
        "weakref_slot": shape.weakref_slot,
    }
    if any(
        getattr(parameters, name, None) is not expected
        for name, expected in expected_parameters.items()
    ):
        return False
    runtime_fields = dataclass_fields(value)
    if len(runtime_fields) != len(shape.fields):
        return False
    for runtime_field, expected_field in zip(
        runtime_fields,
        shape.fields,
        strict=True,
    ):
        has_default = (
            runtime_field.default is not MISSING
            or runtime_field.default_factory is not MISSING
        )
        if (
            runtime_field.name != expected_field.name
            or has_default is not expected_field.has_default
            or runtime_field.init is not expected_field.init
            or runtime_field.repr is not expected_field.repr
            or runtime_field.compare is not expected_field.compare
            or runtime_field.hash is not expected_field.hash_value
            or runtime_field.kw_only is not expected_field.kw_only
        ):
            return False
    for member_name, expected_codes in shape.generated_codes:
        observed = _static_functions(vars(value).get(member_name))
        if not observed or tuple(
            _code_fingerprint(function.__code__) for function in observed
        ) != expected_codes:
            return False
        expected_callables = shape.expected_callables(member_name)
        if expected_callables is None or len(observed) != len(
            expected_callables
        ):
            return False
        for function, expected_callable in zip(
            observed,
            expected_callables,
            strict=True,
        ):
            if not _generated_callable_matches(
                function,
                expected_callable,
                prototype_class=shape.prototype_class,
                runtime_class=value,
                compare_defaults=not any(
                    field.has_default for field in shape.fields
                ),
            ):
                return False
    return True


@dataclass(frozen=True, slots=True)
class _RuntimeClassIdentity:
    qualname: str
    value: type[object]
    mro_identity: tuple[type[object], ...]
    own_members: tuple[tuple[str, object], ...]
    effective_members: tuple[tuple[str, object], ...]
    callable_states: tuple[_RuntimeCallableState, ...]

    @classmethod
    def capture(
        cls,
        qualname: str,
        value: type[object],
    ) -> _RuntimeClassIdentity:
        own_members = tuple(
            sorted(vars(value).items(), key=lambda item: item[0])
        )
        callables: list[_RuntimeCallableState] = []
        for _, member in own_members:
            callables.extend(
                _RuntimeCallableState.capture(function)
                for function in _static_functions(member)
            )
        return cls(
            qualname=qualname,
            value=value,
            mro_identity=cast(tuple[type[object], ...], value.__mro__),
            own_members=own_members,
            effective_members=_effective_class_members(value),
            callable_states=tuple(callables),
        )

    def is_current(self, anchor: _ModuleSourceCodeAnchor) -> bool:
        value = self.value
        shape = anchor.dataclass_shape(self.qualname)
        return (
            _resolve_static_object(
                cast(ModuleType, sys.modules[anchor.module_name]),
                self.qualname,
            )
            is value
            and value.__module__ == anchor.module_name
            and value.__qualname__ == self.qualname
            and value.__mro__ == self.mro_identity
            and _member_identities_match(
                tuple(
                    sorted(vars(value).items(), key=lambda item: item[0])
                ),
                self.own_members,
            )
            and _member_identities_match(
                _effective_class_members(value),
                self.effective_members,
            )
            and all(state.is_current() for state in self.callable_states)
            and (
                shape is None
                or _dataclass_shape_is_current(value, shape)
            )
        )


@dataclass(frozen=True, slots=True)
class _RuntimeModuleCodeBinding:
    anchor: _ModuleSourceCodeAnchor
    module: ModuleType
    spec_identity: ModuleSpec | None
    loader_identity: object
    functions: tuple[_RuntimeFunctionIdentity, ...]
    resolved_functions: tuple[tuple[str, object], ...]
    classes: tuple[_RuntimeClassIdentity, ...]
    complete_at_capture: bool

    @classmethod
    def capture(
        cls,
        module: ModuleType,
        anchor: _ModuleSourceCodeAnchor,
    ) -> _RuntimeModuleCodeBinding:
        functions: list[_RuntimeFunctionIdentity] = []
        resolved_functions: list[tuple[str, object]] = []
        classes: list[_RuntimeClassIdentity] = []
        complete = True
        for qualname, _ in anchor.function_defaults:
            resolved = _resolve_static_object(module, qualname)
            observed = _static_functions(resolved)
            if not observed:
                complete = False
                continue
            resolved_functions.append((qualname, resolved))
            functions.extend(
                _RuntimeFunctionIdentity.capture(qualname, function)
                for function in observed
            )
        for qualname in anchor.class_qualnames:
            resolved = _resolve_static_object(module, qualname)
            if not isinstance(resolved, type):
                complete = False
                continue
            classes.append(
                _RuntimeClassIdentity.capture(qualname, resolved)
            )
        return cls(
            anchor=anchor,
            module=module,
            spec_identity=module.__spec__,
            loader_identity=module.__loader__,
            functions=tuple(functions),
            resolved_functions=tuple(resolved_functions),
            classes=tuple(classes),
            complete_at_capture=complete,
        )

    def is_current(self) -> bool:
        if (
            not self.complete_at_capture
            or self.module.__spec__ is not self.spec_identity
            or self.module.__loader__ is not self.loader_identity
            or not self.anchor.matches_module(self.module)
        ):
            return False
        captured_by_qualname: dict[str, list[object]] = {}
        for identity in self.functions:
            captured_by_qualname.setdefault(identity.qualname, []).append(
                identity.function
            )
            if not identity.is_current(self.module, self.anchor):
                return False
        if not all(
            _resolve_static_object(self.module, qualname) is resolved
            for qualname, resolved in self.resolved_functions
        ):
            return False
        for local_name, module_name, exported_name in self.anchor.core_imports:
            imported_module = sys.modules.get(module_name)
            if type(imported_module) is not ModuleType:
                return False
            expected = (
                imported_module
                if not exported_name
                else imported_module.__dict__.get(exported_name)
            )
            if self.module.__dict__.get(local_name) is not expected:
                return False
        for qualname, captured in captured_by_qualname.items():
            if _static_functions(
                _resolve_static_object(self.module, qualname)
            ) != tuple(captured):
                return False
        return all(identity.is_current(self.anchor) for identity in self.classes)


_CAPABILITY_HOST_ROOT = Path(__file__).resolve(strict=True).parent
_RETRIEVAL_APPROVED_ROOTS_ANCHOR = _TrackedFileAnchor.capture(
    _CAPABILITY_HOST_ROOT / _RETRIEVAL_APPROVED_ROOTS_RELATIVE_PATH
)


def _retrieval_build_modules(anchor: _TrackedFileAnchor) -> tuple[str, ...]:
    try:
        payload = json.loads(anchor.content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Gate C approved roots JSON is invalid") from None
    if type(payload) is not dict:
        raise RuntimeError("Gate C approved roots must be an object")
    raw_paths = cast(dict[str, object], payload).get("build_paths")
    if type(raw_paths) is not list or not raw_paths:
        raise RuntimeError("Gate C build paths must be a non-empty list")
    modules: list[str] = []
    for raw_path in cast(list[object], raw_paths):
        if type(raw_path) is not str:
            raise RuntimeError("Gate C build path must be a string")
        path = Path(cast(str, raw_path))
        if (
            path.parent != Path(".")
            or path.suffix != ".py"
            or not path.stem.startswith("tm_")
        ):
            raise RuntimeError("Gate C build module path is unsupported")
        modules.append(path.stem)
    if len(set(modules)) != len(modules):
        raise RuntimeError("Gate C build modules must be unique")
    return tuple(modules)


_RETRIEVAL_BUILD_MODULE_NAMES = _retrieval_build_modules(
    _RETRIEVAL_APPROVED_ROOTS_ANCHOR
)
_RETRIEVAL_VALIDATION_MODULE_ANCHOR = _ModuleSourceCodeAnchor.capture(
    module_name=_RETRIEVAL_VALIDATION_MODULE_NAME,
    path=_CAPABILITY_HOST_ROOT / f"{_RETRIEVAL_VALIDATION_MODULE_NAME}.py",
    named_defaults={
        "_DEFAULT_APPROVED_ROOTS": _RETRIEVAL_APPROVED_ROOTS_ANCHOR.path,
    },
)


def _loaded_module_binding(
    anchor: _ModuleSourceCodeAnchor,
) -> _RuntimeModuleCodeBinding:
    module = sys.modules.get(anchor.module_name)
    if type(module) is not ModuleType:
        raise RuntimeError("required Core runtime module is not loaded")
    return _RuntimeModuleCodeBinding.capture(module, anchor)


_RETRIEVAL_BUILD_MODULE_ANCHORS = tuple(
    _RETRIEVAL_VALIDATION_MODULE_ANCHOR
    if module_name == _RETRIEVAL_VALIDATION_MODULE_NAME
    else _ModuleSourceCodeAnchor.capture(
        module_name=module_name,
        path=_CAPABILITY_HOST_ROOT / f"{module_name}.py",
        named_defaults=(
            {
                "_DEFAULT_APPROVED_ROOTS": (
                    _CAPABILITY_HOST_ROOT
                    / "tests"
                    / "fixtures"
                    / "feature5_gate_a_v1.json"
                )
            }
            if module_name == "tm_gate_a"
            else None
        ),
    )
    for module_name in _RETRIEVAL_BUILD_MODULE_NAMES
)
_RETRIEVAL_RUNTIME_MODULE_BINDINGS = tuple(
    _loaded_module_binding(anchor)
    for anchor in _RETRIEVAL_BUILD_MODULE_ANCHORS
    if anchor.module_name != _RETRIEVAL_VALIDATION_MODULE_NAME
)

_GATE_D_CONTRACT_ANCHOR = _TrackedFileAnchor.capture(
    _CAPABILITY_HOST_ROOT / _GATE_D_CONTRACT_RELATIVE_PATH
)
_GATE_D_MODULE_ANCHORS = tuple(
    _ModuleSourceCodeAnchor.capture(
        module_name=module_name,
        path=_CAPABILITY_HOST_ROOT / f"{module_name}.py",
        named_defaults=(
            {
                "BENCHMARK_PERCENTILE_METHOD": "nearest-rank",
                "DEFAULT_TIMING_CLOCK_NAME": "perf_counter_ns",
                "time.perf_counter_ns": time.perf_counter_ns,
            }
            if module_name == "tm_benchmark_latency"
            else None
        ),
    )
    for module_name in _GATE_D_MODULE_NAMES
)


@dataclass(frozen=True, slots=True)
class _CoreMatcherFactoryBinding:
    function: FunctionType
    code: CodeType
    module: ModuleType
    module_name: str
    function_name: str
    function_qualname: str
    globals_identity: dict[str, object]
    global_bindings: tuple[tuple[str, object], ...]
    defaults_identity: tuple[object, ...] | None
    defaults_snapshot: tuple[tuple[str, str], ...] | None
    kwdefaults_identity: dict[str, object] | None
    kwdefaults_snapshot: tuple[tuple[str, str, str], ...] | None
    closure_identity: tuple[object, ...] | None
    closure_snapshot: tuple[tuple[int, int, str, str], ...] | None
    source: _PathIdentity
    approved_roots: _PathIdentity

    @classmethod
    def capture(cls, value: object) -> _CoreMatcherFactoryBinding:
        if type(value) is not FunctionType:
            raise RuntimeError("Core matcher factory must be a Python function")
        function = cast(FunctionType, value)
        module = sys.modules.get(function.__module__)
        if type(module) is not ModuleType:
            raise RuntimeError("Core matcher factory module must be loaded")
        if module.__dict__ is not function.__globals__:
            raise RuntimeError("Core matcher factory globals must match its module")
        if getattr(module, function.__name__, None) is not function:
            raise RuntimeError("Core matcher factory module binding is foreign")
        source_path = Path(function.__code__.co_filename).resolve(strict=True)
        module_path = Path(cast(str, module.__file__)).resolve(strict=True)
        if source_path != module_path:
            raise RuntimeError("Core matcher factory source identity is foreign")
        kwdefaults = function.__kwdefaults__
        approved_raw = (
            kwdefaults.get("approved_roots_path")
            if kwdefaults is not None
            else None
        )
        if not isinstance(approved_raw, Path):
            raise RuntimeError("Core matcher factory approved roots are missing")
        closure = cast(tuple[object, ...] | None, function.__closure__)
        global_bindings = tuple(
            sorted(
                (
                    (name, function.__globals__[name])
                    for name in function.__code__.co_names
                    if name in function.__globals__
                ),
                key=lambda item: item[0],
            )
        )
        return cls(
            function=function,
            code=function.__code__,
            module=module,
            module_name=module.__name__,
            function_name=function.__name__,
            function_qualname=function.__qualname__,
            globals_identity=function.__globals__,
            global_bindings=global_bindings,
            defaults_identity=function.__defaults__,
            defaults_snapshot=_function_defaults_snapshot(
                function.__defaults__
            ),
            kwdefaults_identity=kwdefaults,
            kwdefaults_snapshot=_function_kwdefaults_snapshot(kwdefaults),
            closure_identity=closure,
            closure_snapshot=_function_closure_snapshot(closure),
            source=_PathIdentity.capture(source_path, directory=False),
            approved_roots=_PathIdentity.capture(
                approved_raw,
                directory=False,
            ),
        )

    def is_current(self) -> bool:
        function = self.function
        try:
            source_path = Path(function.__code__.co_filename).resolve(
                strict=True
            )
            module_path = Path(cast(str, self.module.__file__)).resolve(
                strict=True
            )
        except (OSError, TypeError, ValueError):
            return False
        return (
            globals().get("build_validated_matcher_v1") is function
            and self.module.__name__ == self.module_name
            and sys.modules.get(self.module_name) is self.module
            and function.__name__ == self.function_name
            and function.__qualname__ == self.function_qualname
            and getattr(self.module, self.function_name, None) is function
            and type(function) is FunctionType
            and function.__code__ is self.code
            and function.__module__ == self.module_name
            and function.__globals__ is self.globals_identity
            and self.module.__dict__ is self.globals_identity
            and all(
                function.__globals__.get(name) is value
                for name, value in self.global_bindings
            )
            and function.__defaults__ is self.defaults_identity
            and _function_defaults_snapshot(function.__defaults__)
            == self.defaults_snapshot
            and function.__kwdefaults__ is self.kwdefaults_identity
            and _function_kwdefaults_snapshot(function.__kwdefaults__)
            == self.kwdefaults_snapshot
            and function.__closure__ is self.closure_identity
            and _function_closure_snapshot(
                cast(tuple[object, ...] | None, function.__closure__)
            )
            == self.closure_snapshot
            and source_path == self.source.path
            and module_path == self.source.path
            and self.source.is_current()
            and self.approved_roots.is_current()
        )

    def invoke(
        self,
        *,
        repository_root: Path,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
        include_full: bool,
    ) -> CapabilityGatedTextMatcherV1 | None:
        if not self.is_current():
            return None
        result = self.function(
            repository_root=repository_root,
            approved_roots_path=self.approved_roots.path,
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
            evaluated_at_utc=evaluated_at_utc,
            include_full=include_full,
        )
        if not self.is_current():
            return None
        return result


_CORE_MATCHER_FACTORY_BINDING = _CoreMatcherFactoryBinding.capture(
    build_validated_matcher_v1
)
_APPLICATION_CHECKOUT_IDENTITY = _ApplicationCheckoutIdentity.capture(
    _CORE_MATCHER_FACTORY_BINDING.source,
    _CORE_MATCHER_FACTORY_BINDING.approved_roots,
)


@dataclass(frozen=True, slots=True)
class _CoreTypeBinding:
    """Import-time identity of one Core constructor or frozen value type."""

    host_name: str | None
    value: type[object]
    module: ModuleType
    module_name: str
    value_name: str
    source: _PathIdentity

    @classmethod
    def capture(
        cls,
        value: type[object],
        *,
        host_name: str | None,
    ) -> _CoreTypeBinding:
        if type(value) is not type:
            raise RuntimeError("Core constructor binding must be a class")
        module = sys.modules.get(value.__module__)
        if type(module) is not ModuleType:
            raise RuntimeError("Core constructor module must be loaded")
        if getattr(module, value.__name__, None) is not value:
            raise RuntimeError("Core constructor module binding is foreign")
        module_path = Path(cast(str, module.__file__)).resolve(strict=True)
        return cls(
            host_name=host_name,
            value=value,
            module=module,
            module_name=module.__name__,
            value_name=value.__name__,
            source=_PathIdentity.capture(module_path, directory=False),
        )

    def is_current(self) -> bool:
        try:
            module_path = Path(cast(str, self.module.__file__)).resolve(
                strict=True
            )
        except (OSError, TypeError, ValueError):
            return False
        return (
            (
                self.host_name is None
                or globals().get(self.host_name) is self.value
            )
            and self.value.__module__ == self.module_name
            and self.value.__name__ == self.value_name
            and sys.modules.get(self.module_name) is self.module
            and getattr(self.module, self.value_name, None) is self.value
            and module_path == self.source.path
            and self.source.is_current()
        )


@dataclass(frozen=True, slots=True)
class _CoreRetrievalValidationBinding:
    """Pinned Core Gate C recomputation and construction graph."""

    function: FunctionType
    code: CodeType
    module: ModuleType
    module_name: str
    function_name: str
    function_qualname: str
    globals_identity: dict[str, object]
    global_bindings: tuple[tuple[str, object], ...]
    defaults_identity: tuple[object, ...] | None
    defaults_snapshot: tuple[tuple[str, str], ...] | None
    kwdefaults_identity: dict[str, object] | None
    kwdefaults_snapshot: tuple[tuple[str, str, str], ...] | None
    closure_identity: tuple[object, ...] | None
    closure_snapshot: tuple[tuple[int, int, str, str], ...] | None
    source: _PathIdentity
    approved_roots: _PathIdentity
    approved_roots_anchor: _TrackedFileAnchor
    validator_graph: _RuntimeModuleCodeBinding
    core_graphs: tuple[_RuntimeModuleCodeBinding, ...]
    release_type: _CoreTypeBinding
    expectation_type: _CoreTypeBinding
    manifest_type: _CoreTypeBinding
    evaluator_type: _CoreTypeBinding
    publisher_type: _CoreTypeBinding
    snapshot_type: _CoreTypeBinding
    service_type: _CoreTypeBinding

    @classmethod
    def capture(
        cls,
        value: object,
        release_type: object,
        *,
        validator_anchor: _ModuleSourceCodeAnchor,
        approved_roots_anchor: _TrackedFileAnchor,
        core_graphs: tuple[_RuntimeModuleCodeBinding, ...],
    ) -> _CoreRetrievalValidationBinding:
        if type(value) is not FunctionType:
            raise RuntimeError("Core Gate C recomputation must be a function")
        if type(release_type) is not type:
            raise RuntimeError("Core Gate C release type must be a class")
        function = cast(FunctionType, value)
        module = sys.modules.get(function.__module__)
        if type(module) is not ModuleType:
            raise RuntimeError("Core Gate C module must be loaded")
        if module.__dict__ is not function.__globals__:
            raise RuntimeError("Core Gate C globals must match its module")
        if getattr(module, function.__name__, None) is not function:
            raise RuntimeError("Core Gate C module binding is foreign")
        source_path = Path(function.__code__.co_filename).resolve(strict=True)
        module_path = Path(cast(str, module.__file__)).resolve(strict=True)
        if source_path != module_path:
            raise RuntimeError("Core Gate C source identity is foreign")
        kwdefaults = function.__kwdefaults__
        approved_raw = (
            kwdefaults.get("approved_roots_path")
            if kwdefaults is not None
            else None
        )
        if not isinstance(approved_raw, Path):
            raise RuntimeError("Core Gate C approved roots are missing")
        closure = cast(tuple[object, ...] | None, function.__closure__)
        global_bindings = tuple(
            sorted(
                (
                    (name, function.__globals__[name])
                    for name in function.__code__.co_names
                    if name in function.__globals__
                ),
                key=lambda item: item[0],
            )
        )
        return cls(
            function=function,
            code=function.__code__,
            module=module,
            module_name=module.__name__,
            function_name=function.__name__,
            function_qualname=function.__qualname__,
            globals_identity=function.__globals__,
            global_bindings=global_bindings,
            defaults_identity=function.__defaults__,
            defaults_snapshot=_function_defaults_snapshot(
                function.__defaults__
            ),
            kwdefaults_identity=kwdefaults,
            kwdefaults_snapshot=_function_kwdefaults_snapshot(kwdefaults),
            closure_identity=closure,
            closure_snapshot=_function_closure_snapshot(closure),
            source=_PathIdentity.capture(source_path, directory=False),
            approved_roots=_PathIdentity.capture(
                approved_raw,
                directory=False,
            ),
            approved_roots_anchor=approved_roots_anchor,
            validator_graph=_RuntimeModuleCodeBinding.capture(
                module,
                validator_anchor,
            ),
            core_graphs=core_graphs,
            release_type=_CoreTypeBinding.capture(
                cast(type[object], release_type),
                host_name=None,
            ),
            expectation_type=_CoreTypeBinding.capture(
                RetrievalCapabilityExpectation,
                host_name="RetrievalCapabilityExpectation",
            ),
            manifest_type=_CoreTypeBinding.capture(
                RetrievalCapabilityManifest,
                host_name="RetrievalCapabilityManifest",
            ),
            evaluator_type=_CoreTypeBinding.capture(
                RetrievalCapabilityEvaluator,
                host_name="RetrievalCapabilityEvaluator",
            ),
            publisher_type=_CoreTypeBinding.capture(
                RetrievalCapabilityPublisher,
                host_name="RetrievalCapabilityPublisher",
            ),
            snapshot_type=_CoreTypeBinding.capture(
                RetrievalCapabilitySnapshot,
                host_name="RetrievalCapabilitySnapshot",
            ),
            service_type=_CoreTypeBinding.capture(
                TMRetrievalService,
                host_name="TMRetrievalService",
            ),
        )

    def is_current(self) -> bool:
        function = self.function
        try:
            source_path = Path(function.__code__.co_filename).resolve(
                strict=True
            )
            module_path = Path(cast(str, self.module.__file__)).resolve(
                strict=True
            )
        except (OSError, TypeError, ValueError):
            return False
        return (
            self.module.__name__ == self.module_name
            and sys.modules.get(self.module_name) is self.module
            and function.__name__ == self.function_name
            and function.__qualname__ == self.function_qualname
            and getattr(self.module, self.function_name, None) is function
            and type(function) is FunctionType
            and function.__code__ is self.code
            and function.__module__ == self.module_name
            and function.__globals__ is self.globals_identity
            and self.module.__dict__ is self.globals_identity
            and all(
                function.__globals__.get(name) is value
                for name, value in self.global_bindings
            )
            and function.__defaults__ is self.defaults_identity
            and _function_defaults_snapshot(function.__defaults__)
            == self.defaults_snapshot
            and function.__kwdefaults__ is self.kwdefaults_identity
            and _function_kwdefaults_snapshot(function.__kwdefaults__)
            == self.kwdefaults_snapshot
            and function.__closure__ is self.closure_identity
            and _function_closure_snapshot(
                cast(tuple[object, ...] | None, function.__closure__)
            )
            == self.closure_snapshot
            and source_path == self.source.path
            and module_path == self.source.path
            and self.source.path == self.validator_graph.anchor.source.path
            and self.approved_roots.path
            == self.approved_roots_anchor.path
            and self.source.is_current()
            and self.approved_roots.is_current()
            and self.approved_roots_anchor.is_current()
            and self.validator_graph.is_current()
            and all(graph.is_current() for graph in self.core_graphs)
            and self.release_type.is_current()
            and self.expectation_type.is_current()
            and self.manifest_type.is_current()
            and self.evaluator_type.is_current()
            and self.publisher_type.is_current()
            and self.snapshot_type.is_current()
            and self.service_type.is_current()
        )

    def recompute(
        self,
        *,
        repository_root: Path,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
    ) -> object | None:
        if not self.is_current():
            return None
        value = self.function(
            repository_root=repository_root,
            approved_roots_path=self.approved_roots.path,
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
        )
        if not self.is_current() or type(value) is not self.release_type.value:
            return None
        return value

    def compose_service(
        self,
        release: object,
        *,
        evaluated_at_utc: datetime,
    ) -> tuple[
        RetrievalCapabilityPublisher,
        TMRetrievalService,
        RetrievalCapabilitySnapshot,
        RetrievalCapabilityManifest,
    ] | None:
        if not self.is_current():
            return None
        if type(release) is not self.release_type.value:
            return None
        expectation = cast(Any, release).expectation
        manifest = cast(Any, release).manifest
        if (
            type(expectation) is not self.expectation_type.value
            or type(manifest) is not self.manifest_type.value
        ):
            return None
        evaluator = cast(Any, self.evaluator_type.value)(expectation)
        if type(evaluator) is not self.evaluator_type.value:
            return None
        publisher = cast(Any, self.publisher_type.value)(
            evaluator,
            initial_manifest=None,
            evaluated_at_utc=evaluated_at_utc,
        )
        if type(publisher) is not self.publisher_type.value:
            return None
        initial = cast(RetrievalCapabilityPublisher, publisher).snapshot()
        initial_fts5, _ = initial.fuzzy_available_for("FTS5_TRIGRAM")
        initial_fallback, _ = initial.fuzzy_available_for("GRAM_FALLBACK")
        if initial.context.available or initial_fts5 or initial_fallback:
            return None
        service = cast(Any, self.service_type.value)(
            capability_publisher=publisher,
        )
        if type(service) is not self.service_type.value:
            return None
        snapshot = cast(RetrievalCapabilityPublisher, publisher).refresh(
            cast(RetrievalCapabilityManifest, manifest),
            evaluated_at_utc=evaluated_at_utc,
        )
        if type(snapshot) is not self.snapshot_type.value:
            return None
        if not self.is_current():
            return None
        return (
            cast(RetrievalCapabilityPublisher, publisher),
            cast(TMRetrievalService, service),
            snapshot,
            cast(RetrievalCapabilityManifest, manifest),
        )


@dataclass(frozen=True, slots=True)
class _RetrievalCheckoutIdentity:
    root: _PathIdentity
    host_module: _PathIdentity
    validation_source: _PathIdentity
    approved_roots: _PathIdentity
    core_sources: tuple[_PathIdentity, ...]

    @classmethod
    def capture(
        cls,
        binding: _CoreRetrievalValidationBinding,
    ) -> _RetrievalCheckoutIdentity:
        host_path = Path(__file__).resolve(strict=True)
        root_path = host_path.parent
        sources = (
            binding.release_type.source,
            binding.expectation_type.source,
            binding.manifest_type.source,
            binding.evaluator_type.source,
            binding.publisher_type.source,
            binding.snapshot_type.source,
            binding.service_type.source,
        )
        if binding.source.path.parent != root_path or any(
            source.path.parent != root_path for source in sources
        ):
            raise RuntimeError(
                "Gate C graph must be loaded from the application checkout"
            )
        try:
            binding.approved_roots.path.relative_to(root_path)
        except ValueError:
            raise RuntimeError(
                "Gate C approved roots must belong to the application checkout"
            ) from None
        return cls(
            root=_PathIdentity.capture(root_path, directory=True),
            host_module=_PathIdentity.capture(host_path, directory=False),
            validation_source=binding.source,
            approved_roots=binding.approved_roots,
            core_sources=sources,
        )

    def is_current(self) -> bool:
        if (
            self.host_module.path.parent != self.root.path
            or self.validation_source.path.parent != self.root.path
            or any(
                source.path.parent != self.root.path
                for source in self.core_sources
            )
        ):
            return False
        try:
            self.approved_roots.path.relative_to(self.root.path)
        except ValueError:
            return False
        return (
            self.root.is_current()
            and self.host_module.is_current()
            and self.validation_source.is_current()
            and self.approved_roots.is_current()
            and all(source.is_current() for source in self.core_sources)
        )


def _load_retrieval_validation_binding(
) -> tuple[_CoreRetrievalValidationBinding, _RetrievalCheckoutIdentity]:
    """Late-bind validator objects under import-time tracked-source anchors."""

    module = importlib.import_module(_RETRIEVAL_VALIDATION_MODULE_NAME)
    if type(module) is not ModuleType:
        raise RuntimeError("Core Gate C validation module must be loaded")
    function = getattr(module, "recompute_retrieval_validation", None)
    release_type = getattr(module, "RetrievalValidationRelease", None)
    binding = _CoreRetrievalValidationBinding.capture(
        function,
        release_type,
        validator_anchor=_RETRIEVAL_VALIDATION_MODULE_ANCHOR,
        approved_roots_anchor=_RETRIEVAL_APPROVED_ROOTS_ANCHOR,
        core_graphs=_RETRIEVAL_RUNTIME_MODULE_BINDINGS,
    )
    return binding, _RetrievalCheckoutIdentity.capture(binding)


@dataclass(frozen=True, slots=True)
class _GateDExecutionResult:
    """Host publication plan fully constructed before Core commit."""

    snapshot: RetrievalCapabilitySnapshot
    handoff: RetrievalHandoffSnapshot
    status: CapabilityDisplaySnapshot

    def __post_init__(self) -> None:
        if type(self.snapshot) is not RetrievalCapabilitySnapshot:
            raise TypeError("Gate D result must carry a Core snapshot")
        if type(self.handoff) is not RetrievalHandoffSnapshot:
            raise TypeError("Gate D result must carry a host handoff")
        if type(self.status) is not CapabilityDisplaySnapshot:
            raise TypeError("Gate D result must carry a host status")
        if self.status.retrieval is not self.handoff.display:
            raise ValueError("Gate D status must retain the handoff display")


class _GateDOperationalError(RuntimeError):
    """One safe operational code from the Gate D owner lifecycle."""

    error_code: str

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("Gate D error code must be a non-empty string")
        self.error_code = error_code
        super().__init__(error_code)


class _GateDExecutionPort(Protocol):
    def run(
        self,
        *,
        contract_path: Path,
        work_root: Path,
        evidence_path: Path,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> _CoreGateDPublication: ...


_GATE_D_PUBLICATION_MINT = object()


def _gate_d_publication_bindings_are_canonical(
    *,
    gate_module: ModuleType,
    retrieval_module: ModuleType,
    bindings: object,
) -> bool:
    """Bind every authority-bearing publication primitive by identity."""

    if type(bindings) is not tuple or len(bindings) != 15:
        return False
    expected = (
        gate_module.__dict__.get("_GATE_D_PUBLICATION_BINDINGS_MINT"),
        gate_module.__dict__.get("BenchmarkGateDError"),
        gate_module.__dict__.get("_verify_path_decisions_match_reports"),
        gate_module.__dict__.get("retrieval_benchmark_evidence_pair"),
        gate_module.__dict__.get("benchmark_implementation_fingerprint"),
        gate_module.__dict__.get("RetrievalCapabilityManifest"),
        gate_module.__dict__.get("CANDIDATE_PROOF_QUERY_VERSION"),
        gate_module.__dict__.get("RetrievalCapabilityPublicationResult"),
        gate_module.__dict__.get("RetrievalCapabilityPublisher"),
        gate_module.__dict__.get("_require_utc_datetime"),
        gate_module.__dict__.get("_GateDRunReceipt"),
        gate_module.__dict__.get("BenchmarkGateDRunResult"),
        gate_module.__dict__.get("_validate_evidence_utc_string"),
        retrieval_module.__dict__.get(
            "_RETRIEVAL_CAPABILITY_SNAPSHOT_DESCRIPTOR"
        ),
        retrieval_module.__dict__.get(
            "_validated_refresh_retrieval_capability"
        ),
    )
    return all(
        actual is canonical
        for actual, canonical in zip(bindings, expected, strict=True)
    ) and gate_module.__dict__.get(
        "_validated_refresh_retrieval_capability"
    ) is retrieval_module.__dict__.get(
        "_validated_refresh_retrieval_capability"
    )


@dataclass(frozen=True, slots=True)
class _CoreGateDBinding:
    """Late-bound current-checkout Core Gate D owner graph."""

    graphs: tuple[_RuntimeModuleCodeBinding, ...]
    gate_module: ModuleType
    run_function: FunctionType
    publish_function: FunctionType
    persist_attestation_function: FunctionType
    restore_attestation_function: FunctionType
    error_type: type[BaseException]
    run_result_type: type[object]
    publication_result_type: type[object]
    publication_bindings: tuple[object, ...]

    @classmethod
    def capture(cls) -> _CoreGateDBinding:
        modules = tuple(
            importlib.import_module(anchor.module_name)
            for anchor in _GATE_D_MODULE_ANCHORS
        )
        if any(type(module) is not ModuleType for module in modules):
            raise RuntimeError("Gate D Core modules must be canonical modules")
        graphs = tuple(
            _RuntimeModuleCodeBinding.capture(
                cast(ModuleType, module),
                anchor,
            )
            for module, anchor in zip(
                modules,
                _GATE_D_MODULE_ANCHORS,
                strict=True,
            )
        )
        gate_module = cast(ModuleType, modules[-1])
        retrieval_module = cast(ModuleType, modules[-2])
        run_function = gate_module.__dict__.get("run_benchmark_gate_d")
        publish_function = gate_module.__dict__.get(
            "_publish_retrieval_capability_gate_d_prepared"
        )
        persist_attestation_function = gate_module.__dict__.get(
            "_persist_gate_d_attestation"
        )
        restore_attestation_function = gate_module.__dict__.get(
            "_restore_gate_d_attestation"
        )
        error_type = gate_module.__dict__.get("BenchmarkGateDError")
        run_result_type = gate_module.__dict__.get(
            "BenchmarkGateDRunResult"
        )
        publication_result_type = gate_module.__dict__.get(
            "RetrievalCapabilityPublicationResult"
        )
        publication_bindings = gate_module.__dict__.get(
            "_GATE_D_PUBLICATION_BINDINGS"
        )
        if (
            type(run_function) is not FunctionType
            or type(publish_function) is not FunctionType
            or type(persist_attestation_function) is not FunctionType
            or type(restore_attestation_function) is not FunctionType
            or type(error_type) is not type
            or not issubclass(cast(type[object], error_type), BaseException)
            or type(run_result_type) is not type
            or type(publication_result_type) is not type
            or not _gate_d_publication_bindings_are_canonical(
                gate_module=gate_module,
                retrieval_module=retrieval_module,
                bindings=publication_bindings,
            )
        ):
            raise RuntimeError("Gate D Core owner exports are invalid")
        binding = cls(
            graphs=graphs,
            gate_module=gate_module,
            run_function=cast(FunctionType, run_function),
            publish_function=cast(FunctionType, publish_function),
            persist_attestation_function=cast(
                FunctionType,
                persist_attestation_function,
            ),
            restore_attestation_function=cast(
                FunctionType,
                restore_attestation_function,
            ),
            error_type=cast(type[BaseException], error_type),
            run_result_type=cast(type[object], run_result_type),
            publication_result_type=cast(
                type[object], publication_result_type
            ),
            publication_bindings=cast(
                tuple[object, ...], publication_bindings
            ),
        )
        if not binding.is_current():
            raise RuntimeError("Gate D Core owner graph is not current")
        return binding

    def is_current(self) -> bool:
        return (
            _GATE_D_CONTRACT_ANCHOR.is_current()
            and all(graph.is_current() for graph in self.graphs)
            and self.gate_module.__dict__.get("run_benchmark_gate_d")
            is self.run_function
            and self.gate_module.__dict__.get(
                "_publish_retrieval_capability_gate_d_prepared"
            )
            is self.publish_function
            and self.gate_module.__dict__.get(
                "_persist_gate_d_attestation"
            )
            is self.persist_attestation_function
            and self.gate_module.__dict__.get(
                "_restore_gate_d_attestation"
            )
            is self.restore_attestation_function
            and self.gate_module.__dict__.get("BenchmarkGateDError")
            is self.error_type
            and self.gate_module.__dict__.get("BenchmarkGateDRunResult")
            is self.run_result_type
            and self.gate_module.__dict__.get(
                "RetrievalCapabilityPublicationResult"
            )
            is self.publication_result_type
            and self.gate_module.__dict__.get(
                "_GATE_D_PUBLICATION_BINDINGS"
            )
            is self.publication_bindings
            and _gate_d_publication_bindings_are_canonical(
                gate_module=self.gate_module,
                retrieval_module=self.graphs[-2].module,
                bindings=self.publication_bindings,
            )
        )

    def run(
        self,
        *,
        contract_path: Path,
        work_root: Path,
        evidence_path: Path,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> _CoreGateDPublication:
        if (
            not self.is_current()
            or contract_path != _GATE_D_CONTRACT_ANCHOR.path
        ):
            raise _GateDOperationalError("GATE_D.IMPLEMENTATION_CHANGED")
        try:
            run_result = self.run_function(
                contract_path,
                work_root,
                evidence_path,
            )
        except self.error_type as error:
            raise _GateDOperationalError(
                cast(Any, error).error_code
            ) from error
        if (
            type(run_result) is not self.run_result_type
            or not self.is_current()
        ):
            raise _GateDOperationalError("GATE_D.IMPLEMENTATION_CHANGED")
        return _CoreGateDPublication(
            _mint=_GATE_D_PUBLICATION_MINT,
            binding=self,
            run_result=run_result,
            publication_owner_identity=publication_owner_identity,
            publication_graph_nonce=publication_graph_nonce,
        )

    def publish(
        self,
        *,
        run_result: object,
        base_manifest: RetrievalCapabilityManifest,
        publisher: RetrievalCapabilityPublisher,
        evaluated_at_utc: datetime,
        prepare_publication: Callable[
            [RetrievalCapabilitySnapshot],
            _GateDExecutionResult,
        ],
    ) -> _GateDExecutionResult:
        if (
            type(run_result) is not self.run_result_type
            or not self.is_current()
        ):
            raise _GateDOperationalError("GATE_D.IMPLEMENTATION_CHANGED")
        def prepare_core_result(publication: object) -> _GateDExecutionResult:
            if type(publication) is not self.publication_result_type:
                raise _GateDOperationalError(
                    "GATE_D.IMPLEMENTATION_CHANGED"
                )
            validated_publication: Any = publication
            snapshot = validated_publication.snapshot
            if type(snapshot) is not RetrievalCapabilitySnapshot:
                raise TypeError(
                    "Core Gate D publication returned invalid snapshot"
                )
            return prepare_publication(snapshot)

        try:
            publication = self.publish_function(
                base_manifest,
                run_result,
                publisher,
                generated_at_utc=base_manifest.generated_at_utc,
                valid_until_utc=base_manifest.valid_until_utc,
                evaluated_at_utc=evaluated_at_utc,
                prepare_publication=prepare_core_result,
                _publication_bindings=self.publication_bindings,
            )
        except self.error_type as error:
            validated_error: Any = error
            raise _GateDOperationalError(
                validated_error.error_code
            ) from error
        validated_result: Any = publication
        return validated_result

    def persist_attestation(
        self,
        *,
        run_result: object,
        contract_path: Path,
        state_root: Path,
        base_manifest: RetrievalCapabilityManifest,
        issued_at_utc: datetime,
    ) -> None:
        if (
            type(run_result) is not self.run_result_type
            or not self.is_current()
            or contract_path != _GATE_D_CONTRACT_ANCHOR.path
        ):
            raise _GateDOperationalError("GATE_D.IMPLEMENTATION_CHANGED")
        try:
            self.persist_attestation_function(
                contract_path=contract_path,
                state_root=state_root,
                base_manifest=base_manifest,
                run_result=run_result,
                issued_at_utc=issued_at_utc,
            )
        except self.error_type as error:
            validated_error: Any = error
            raise _GateDOperationalError(
                validated_error.error_code
            ) from error

    def restore(
        self,
        *,
        contract_path: Path,
        state_root: Path,
        base_manifest: RetrievalCapabilityManifest,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> _CoreGateDPublication:
        if (
            not self.is_current()
            or contract_path != _GATE_D_CONTRACT_ANCHOR.path
        ):
            raise _GateDOperationalError("GATE_D.IMPLEMENTATION_CHANGED")
        try:
            run_result = self.restore_attestation_function(
                contract_path=contract_path,
                state_root=state_root,
                base_manifest=base_manifest,
            )
        except self.error_type as error:
            validated_error: Any = error
            raise _GateDOperationalError(
                validated_error.error_code
            ) from error
        if (
            type(run_result) is not self.run_result_type
            or not self.is_current()
        ):
            raise _GateDOperationalError("GATE_D.IMPLEMENTATION_CHANGED")
        return _CoreGateDPublication(
            _mint=_GATE_D_PUBLICATION_MINT,
            binding=self,
            run_result=run_result,
            publication_owner_identity=publication_owner_identity,
            publication_graph_nonce=publication_graph_nonce,
        )


@final
class _CoreGateDPublication:
    """Pinned Core run receipt whose only mutation is formal publication."""

    __slots__ = (
        "__binding",
        "__consume_lock",
        "__consumed",
        "__publication_graph_nonce",
        "__publication_owner_identity",
        "__run_result",
    )

    def __init__(
        self,
        *,
        _mint: object,
        binding: _CoreGateDBinding,
        run_result: object,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> None:
        if _mint is not _GATE_D_PUBLICATION_MINT:
            raise PermissionError("Gate D publication mint is private")
        if type(binding) is not _CoreGateDBinding:
            raise TypeError("Gate D publication requires the Core binding")
        if type(run_result) is not binding.run_result_type:
            raise TypeError("Gate D publication requires a Core run result")
        if type(publication_owner_identity) is not object:
            raise TypeError("Gate D publication owner identity is invalid")
        if type(publication_graph_nonce) is not object:
            raise TypeError("Gate D publication graph nonce is invalid")
        self.__binding = binding
        self.__run_result = run_result
        self.__publication_owner_identity = publication_owner_identity
        self.__publication_graph_nonce = publication_graph_nonce
        self.__consume_lock = Lock()
        self.__consumed = False

    def publish(
        self,
        *,
        publication_owner_identity: object,
        publication_graph_nonce: object,
        base_manifest: RetrievalCapabilityManifest,
        publisher: RetrievalCapabilityPublisher,
        evaluated_at_utc: datetime,
        prepare_publication: Callable[
            [RetrievalCapabilitySnapshot],
            _GateDExecutionResult,
        ],
    ) -> _GateDExecutionResult:
        if (
            publication_owner_identity
            is not self.__publication_owner_identity
            or publication_graph_nonce is not self.__publication_graph_nonce
        ):
            raise PermissionError(
                "Gate D publication receipt belongs to another graph"
            )
        with self.__consume_lock:
            if self.__consumed:
                raise RuntimeError(
                    "Gate D publication receipt was already consumed"
                )
            self.__consumed = True
            return self.__binding.publish(
                run_result=self.__run_result,
                base_manifest=base_manifest,
                publisher=publisher,
                evaluated_at_utc=evaluated_at_utc,
                prepare_publication=prepare_publication,
            )

    def persist_attestation(
        self,
        *,
        contract_path: Path,
        state_root: Path,
        base_manifest: RetrievalCapabilityManifest,
        issued_at_utc: datetime,
    ) -> None:
        with self.__consume_lock:
            if self.__consumed:
                raise RuntimeError(
                    "Gate D publication receipt was already consumed"
                )
            self.__binding.persist_attestation(
                run_result=self.__run_result,
                contract_path=contract_path,
                state_root=state_root,
                base_manifest=base_manifest,
                issued_at_utc=issued_at_utc,
            )


@final
class _RealGateDExecution:
    """Load and invoke the pinned offline Core owner on the worker thread."""

    __slots__ = ()

    def run(
        self,
        *,
        contract_path: Path,
        work_root: Path,
        evidence_path: Path,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> _CoreGateDPublication:
        try:
            binding = _CoreGateDBinding.capture()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise _GateDOperationalError(
                "GATE_D.IMPLEMENTATION_CHANGED"
            ) from error
        return binding.run(
            contract_path=contract_path,
            work_root=work_root,
            evidence_path=evidence_path,
            publication_owner_identity=publication_owner_identity,
            publication_graph_nonce=publication_graph_nonce,
        )

    def restore(
        self,
        *,
        contract_path: Path,
        state_root: Path,
        base_manifest: RetrievalCapabilityManifest,
        publication_owner_identity: object,
        publication_graph_nonce: object,
    ) -> _CoreGateDPublication:
        try:
            binding = _CoreGateDBinding.capture()
        except (ImportError, OSError, RuntimeError, ValueError) as error:
            raise _GateDOperationalError(
                "GATE_D.IMPLEMENTATION_CHANGED"
            ) from error
        return binding.restore(
            contract_path=contract_path,
            state_root=state_root,
            base_manifest=base_manifest,
            publication_owner_identity=publication_owner_identity,
            publication_graph_nonce=publication_graph_nonce,
        )


_REAL_GATE_D_EXECUTION = _RealGateDExecution()
_REAL_GATE_D_EXECUTE = _REAL_GATE_D_EXECUTION.run
_REAL_GATE_D_RESTORE = _REAL_GATE_D_EXECUTION.restore


class GateDRunState(Enum):
    """Safe lifecycle state; it never carries paths or evidence."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class GateDRunStatus:
    """Safe immutable status of the process-local Gate D owner."""

    epoch: int
    state: GateDRunState
    safe_code: str | None

    def __post_init__(self) -> None:
        _require_generation(self.epoch)
        if type(self.state) is not GateDRunState:
            raise TypeError("Gate D state must be GateDRunState")
        if self.state is GateDRunState.FAILED:
            if type(self.safe_code) is not str or not self.safe_code:
                raise ValueError("failed Gate D status requires a safe code")
        elif self.safe_code is not None:
            raise ValueError("non-failed Gate D status cannot carry a code")


@dataclass(frozen=True, slots=True)
class _GateDGraphSnapshot:
    """Private exact Gate C graph captured before one Gate D run."""

    publisher: RetrievalCapabilityPublisher
    service: TMRetrievalService
    base_manifest: RetrievalCapabilityManifest
    handoff: RetrievalHandoffSnapshot
    capability: RetrievalCapabilitySnapshot
    publication_nonce: object


@dataclass(frozen=True, slots=True)
class MatcherHandoffSnapshot:
    """One immutable matcher handoff captured by a search operation."""

    generation: int
    matcher: CapabilityGatedTextMatcher | None
    display: TextMatcherDisplayState

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        if self.matcher is not None:
            if type(self.matcher) is not CapabilityGatedTextMatcherV1:
                raise TypeError(
                    "matcher must be constructed by the Core text-v1 factory"
                )
        if type(self.display) is not TextMatcherDisplayState:
            raise TypeError("matcher display must be TextMatcherDisplayState")
        if self.matcher is None and self.display.state is not TextMatcherState.UNAVAILABLE:
            raise ValueError("missing matcher requires an unavailable display")
        if self.matcher is not None and self.display.state is TextMatcherState.UNAVAILABLE:
            raise ValueError("unavailable matcher display cannot expose a matcher")
        if self.matcher is not None:
            capability = self.matcher.capability()
            if (
                capability.state is not self.display.state
                or capability.supported_profiles
                != self.display.supported_profiles
            ):
                raise ValueError(
                    "matcher display must equal the Core capability snapshot"
                )


@dataclass(frozen=True, slots=True)
class RetrievalHandoffSnapshot:
    """One immutable retrieval query handoff without authority mutation."""

    generation: int
    query_port: RetrievalQueryPort
    display: RetrievalDisplayState

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        if type(self.query_port) is not _CoreRetrievalQueryPort:
            raise TypeError("retrieval query port must be host-owned")
        if type(self.display) is not RetrievalDisplayState:
            raise TypeError("retrieval display must be RetrievalDisplayState")


@dataclass(frozen=True, slots=True)
class _RetrievalOperationResult:
    """Host-private pairing of one exact handoff and its Core report."""

    handoff: RetrievalHandoffSnapshot
    report: QueryReport

    def __post_init__(self) -> None:
        if type(self.handoff) is not RetrievalHandoffSnapshot:
            raise TypeError("retrieval operation handoff must be host-owned")
        if type(self.report) is not QueryReport:
            raise TypeError("retrieval operation report must be QueryReport")


class RetrievalQueryPort(Protocol):
    """Read-only Core retrieval execution port exposed to application code."""

    def query(
        self,
        resources: tuple[TMResourceHandle, ...],
        query: TMQuery,
    ) -> QueryReport: ...


@runtime_checkable
class MatcherGenerationNotificationPort(Protocol):
    """Read-only generation observation for the future Controller adapter."""

    def current(self) -> int: ...

    def wait_for_change(
        self,
        *,
        after_generation: int,
        timeout: float | None = None,
    ) -> int | None: ...


@runtime_checkable
class MatcherValidationOwnerPort(Protocol):
    """Composition-owner-only entry to the Core matcher validation factory."""

    def validate_basic(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> MatcherHandoffSnapshot: ...

    def validate_text_v1(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> MatcherHandoffSnapshot: ...


@runtime_checkable
class RetrievalGenerationNotificationPort(Protocol):
    """Read-only retrieval generation observation for Controller wiring."""

    def current(self) -> int: ...

    def wait_for_change(
        self,
        *,
        after_generation: int,
        timeout: float | None = None,
    ) -> int | None: ...


@runtime_checkable
class RetrievalGateCValidationOwnerPort(Protocol):
    """Composition-owner-only Gate C recomputation entry."""

    def validate_gate_c(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> RetrievalHandoffSnapshot: ...


@runtime_checkable
class RetrievalGateDOwnerPort(Protocol):
    """Composition-owner-only asynchronous Gate D lifecycle."""

    def start_gate_d(
        self,
        *,
        evaluated_at_utc: datetime,
    ) -> GateDRunStatus: ...

    def restore_gate_d(
        self,
        *,
        evaluated_at_utc: datetime,
    ) -> GateDRunStatus: ...

    def status(self) -> GateDRunStatus: ...

    def wait(self, timeout: float | None = None) -> GateDRunStatus: ...


@final
@dataclass(frozen=True, slots=True)
class _CoreRetrievalQueryPort:
    """Keep each mutable Core publisher/service graph host-private."""

    __service: TMRetrievalService  # pyright: ignore[reportGeneralTypeIssues]

    def __post_init__(self) -> None:
        if type(self.__service) is not TMRetrievalService:
            raise TypeError("retrieval service must be TMRetrievalService")

    def query(
        self,
        resources: tuple[TMResourceHandle, ...],
        query: TMQuery,
    ) -> QueryReport:
        """Delegate one query to the Core service's single-snapshot port."""

        return self.__service.query(resources, query)

    def _is_bound_to(self, service: TMRetrievalService) -> bool:
        """Verify host-private service identity without exposing the service."""

        return self.__service is service


@final
class _MatcherGenerationNotifications:
    """Condition-backed observer with no public publication operation."""

    __slots__ = ("__condition", "__generation", "__owner_identity")

    def __init__(self, lock: RLock, owner_identity: object) -> None:
        self.__condition = Condition(lock)
        self.__generation = 0
        self.__owner_identity = owner_identity

    def current(self) -> int:
        with self.__condition:
            return self.__generation

    def wait_for_change(
        self,
        *,
        after_generation: int,
        timeout: float | None = None,
    ) -> int | None:
        _require_generation(after_generation)
        if timeout is not None:
            if type(timeout) not in (int, float):
                raise TypeError("matcher generation timeout must be numeric")
            numeric_timeout = float(timeout)
            if not math.isfinite(numeric_timeout) or numeric_timeout < 0.0:
                raise ValueError(
                    "matcher generation timeout must be finite and non-negative"
                )
        else:
            numeric_timeout = None
        with self.__condition:
            changed = self.__condition.wait_for(
                lambda: self.__generation > after_generation,
                timeout=numeric_timeout,
            )
            if not changed:
                return None
            return self.__generation

    def _publish_locked(
        self,
        owner_identity: object,
        generation: int,
    ) -> None:
        """Publish while CapabilityHost holds the shared re-entrant lock."""

        self._validate_publish_locked(owner_identity, generation)
        self._publish_prevalidated_locked(generation)

    def _validate_publish_locked(
        self,
        owner_identity: object,
        generation: int,
    ) -> None:
        if owner_identity is not self.__owner_identity:
            raise PermissionError(
                "matcher generation publication requires composition owner"
            )
        _require_generation(generation)
        if generation <= self.__generation:
            raise ValueError("matcher generation must increase")

    def _publish_prevalidated_locked(self, generation: int) -> None:
        """Commit a generation already validated under the same host lock."""

        self.__generation = generation
        self.__condition.notify_all()

    def _precommit_snapshot_locked(self) -> int:
        """Capture rollback state while the host owns the condition lock."""

        return self.__generation

    def _restore_precommit_locked(self, generation: int) -> None:
        """Restore captured state directly after a provisional failure."""

        self.__generation = generation


@final
class _RetrievalGenerationNotifications:
    """Condition-backed retrieval observer with no mutation surface."""

    __slots__ = ("__condition", "__generation", "__owner_identity")

    def __init__(self, lock: RLock, owner_identity: object) -> None:
        self.__condition = Condition(lock)
        self.__generation = 0
        self.__owner_identity = owner_identity

    def current(self) -> int:
        with self.__condition:
            return self.__generation

    def wait_for_change(
        self,
        *,
        after_generation: int,
        timeout: float | None = None,
    ) -> int | None:
        _require_generation(after_generation)
        if timeout is not None:
            if type(timeout) not in (int, float):
                raise TypeError("retrieval generation timeout must be numeric")
            numeric_timeout = float(timeout)
            if not math.isfinite(numeric_timeout) or numeric_timeout < 0.0:
                raise ValueError(
                    "retrieval generation timeout must be finite and non-negative"
                )
        else:
            numeric_timeout = None
        with self.__condition:
            changed = self.__condition.wait_for(
                lambda: self.__generation > after_generation,
                timeout=numeric_timeout,
            )
            if not changed:
                return None
            return self.__generation

    def _publish_locked(
        self,
        owner_identity: object,
        generation: int,
    ) -> None:
        self._validate_publish_locked(owner_identity, generation)
        self._publish_prevalidated_locked(generation)

    def _validate_publish_locked(
        self,
        owner_identity: object,
        generation: int,
    ) -> None:
        if owner_identity is not self.__owner_identity:
            raise PermissionError(
                "retrieval generation publication requires composition owner"
            )
        _require_generation(generation)
        if generation <= self.__generation:
            raise ValueError("retrieval generation must increase")

    def _publish_prevalidated_locked(self, generation: int) -> None:
        """Commit a generation already validated under the same host lock."""

        self.__generation = generation
        self.__condition.notify_all()

    def _precommit_snapshot_locked(self) -> int:
        """Capture rollback state while the host owns the condition lock."""

        return self.__generation

    def _restore_precommit_locked(self, generation: int) -> None:
        """Restore captured state directly after a provisional failure."""

        self.__generation = generation


@dataclass(frozen=True, slots=True)
class CapabilityDisplaySnapshot:
    """Safe one-way display projection of both independent authorities."""

    matcher: TextMatcherDisplayState
    retrieval: RetrievalDisplayState

    def __post_init__(self) -> None:
        if type(self.matcher) is not TextMatcherDisplayState:
            raise TypeError("matcher display must be TextMatcherDisplayState")
        if type(self.retrieval) is not RetrievalDisplayState:
            raise TypeError("retrieval display must be RetrievalDisplayState")


def _exact_only_retrieval_display(
    publisher: RetrievalCapabilityPublisher,
) -> RetrievalDisplayState:
    capability = publisher.snapshot()
    display = _retrieval_display(capability)
    if display.context_available or display.fuzzy_available:
        raise RuntimeError("exact-only bootstrap received an open retrieval gate")
    return display


def _retrieval_display(
    capability: RetrievalCapabilitySnapshot,
) -> RetrievalDisplayState:
    if type(capability) is not RetrievalCapabilitySnapshot:
        raise TypeError(
            "retrieval display requires a Core capability snapshot"
        )
    fts5_available, _ = capability.fuzzy_available_for("FTS5_TRIGRAM")
    fallback_available, _ = capability.fuzzy_available_for("GRAM_FALLBACK")
    return RetrievalDisplayState(
        context_available=capability.context.available,
        fuzzy_available=fts5_available or fallback_available,
        safe_codes=capability.summary.unavailable_codes,
    )


def _clone_retrieval_display(
    display: RetrievalDisplayState,
) -> RetrievalDisplayState:
    return RetrievalDisplayState(
        context_available=display.context_available,
        fuzzy_available=display.fuzzy_available,
        safe_codes=tuple(display.safe_codes),
    )


@final
class CapabilityHost:
    """Hold immutable process handoffs for independent matcher/retrieval gates.

    Validation mutation is isolated behind composition-owner objects; the
    ordinary host surface accepts no evidence, manifest, caller flag, or store
    health. Gate C replaces the whole retrieval graph; Gate D may refresh only
    that same graph and republishes a read-only generation handoff.
    """

    __slots__ = (
        "__lock",
        "__matcher_handoff",
        "__matcher_notifications",
        "__matcher_owner_identity",
        "__retrieval_notifications",
        "__retrieval_operation_display",
        "__retrieval_owner_identity",
        "__retrieval_checkout_identity",
        "__retrieval_base_manifest",
        "__retrieval_lifecycle_lock",
        "__retrieval_publisher",
        "__retrieval_service",
        "__retrieval_handoff",
        "__retrieval_validation_binding",
        "__status",
    )

    def __init__(self, *, evaluated_at_utc: datetime) -> None:
        publisher = default_retrieval_capability_publisher(evaluated_at_utc)
        retrieval_display = _exact_only_retrieval_display(publisher)
        service = TMRetrievalService(capability_publisher=publisher)
        retrieval = RetrievalHandoffSnapshot(
            generation=0,
            query_port=_CoreRetrievalQueryPort(service),
            display=retrieval_display,
        )
        matcher_display = TextMatcherDisplayState(
            state=TextMatcherState.UNAVAILABLE,
            supported_profiles=(),
            safe_reason=_MATCHER_CLOSED_REASON,
        )
        matcher = MatcherHandoffSnapshot(
            generation=0,
            matcher=None,
            display=matcher_display,
        )

        self.__lock = RLock()
        self.__retrieval_lifecycle_lock = Lock()
        self.__matcher_handoff = matcher
        self.__matcher_owner_identity = object()
        self.__matcher_notifications = _MatcherGenerationNotifications(
            self.__lock,
            self.__matcher_owner_identity,
        )
        self.__retrieval_owner_identity = object()
        self.__retrieval_checkout_identity: (
            _RetrievalCheckoutIdentity | None
        ) = None
        self.__retrieval_validation_binding: (
            _CoreRetrievalValidationBinding | None
        ) = None
        self.__retrieval_base_manifest: RetrievalCapabilityManifest | None = (
            None
        )
        self.__retrieval_notifications = _RetrievalGenerationNotifications(
            self.__lock,
            self.__retrieval_owner_identity,
        )
        self.__retrieval_publisher = publisher
        self.__retrieval_service = service
        self.__retrieval_handoff = retrieval
        self.__retrieval_operation_display = _clone_retrieval_display(
            retrieval.display
        )
        self.__status = CapabilityDisplaySnapshot(
            matcher=matcher.display,
            retrieval=retrieval.display,
        )

    def matcher_snapshot(self) -> MatcherHandoffSnapshot:
        """Capture one immutable matcher handoff reference."""

        with self.__lock:
            return self.__matcher_handoff

    def _run_if_matcher_handoff_current(
        self,
        candidate: MatcherHandoffSnapshot,
        operation: Callable[[], _OperationResultT],
    ) -> _OperationResultT:
        """Linearize one short application commit against one handoff.

        The exact host-owned snapshot, rather than a caller-supplied
        generation integer, is the reservation token.  Matcher publication
        uses the same lock, so it cannot interleave with the operation.
        """

        if type(candidate) is not MatcherHandoffSnapshot:
            raise TypeError(
                "matcher generation reservation requires a host handoff"
            )
        if not callable(operation):
            raise TypeError("matcher generation operation must be callable")
        with self.__lock:
            candidate.__post_init__()
            if (
                self.__matcher_handoff is not candidate
                or self.__matcher_notifications.current()
                != candidate.generation
            ):
                raise _MatcherGenerationChanged
            return operation()

    def matcher_generation_notifications(
        self,
    ) -> MatcherGenerationNotificationPort:
        """Return the read-only generation-change observation port."""

        return self.__matcher_notifications

    def retrieval_snapshot(self) -> RetrievalHandoffSnapshot:
        """Capture one immutable retrieval handoff reference."""

        with self.__lock:
            return self.__retrieval_handoff

    def retrieval_operation_snapshot(self) -> RetrievalHandoffSnapshot:
        """Return a defensive handoff bound to the current private graph."""

        with self.__lock:
            handoff, _capability = self.__capture_retrieval_operation_locked()
            return handoff

    def _run_if_retrieval_generation_current(
        self,
        generation: int,
        operation: Callable[[], _OperationResultT],
    ) -> _OperationResultT:
        """Run one short application commit against an exact generation."""

        if type(generation) is not int or generation < 0:
            raise TypeError("retrieval generation must be non-negative int")
        if not callable(operation):
            raise TypeError("retrieval generation operation must be callable")
        with self.__retrieval_lifecycle_lock:
            with self.__lock:
                try:
                    handoff, _publisher, _service = (
                        self.__validate_retrieval_graph_locked()
                    )
                except ValueError as error:
                    raise _RetrievalGenerationChanged from error
                if handoff.generation != generation:
                    raise _RetrievalGenerationChanged
                return operation()

    def query_retrieval_operation(
        self,
        resources: tuple[TMResourceHandle, ...],
        query: TMQuery,
    ) -> _RetrievalOperationResult:
        """Execute one query under the exact host publication reservation."""

        with self.__retrieval_lifecycle_lock:
            with self.__lock:
                handoff, _publisher, service = (
                    self.__validate_retrieval_graph_locked()
                )
                reservation = service._reserve_query_operation()
                capability = reservation.capability_snapshot
                if (
                    _retrieval_display(capability)
                    != self.__retrieval_operation_display
                ):
                    raise ValueError("retrieval capability drift")
                operation_handoff = RetrievalHandoffSnapshot(
                    generation=handoff.generation,
                    query_port=_CoreRetrievalQueryPort(service),
                    display=_clone_retrieval_display(
                        self.__retrieval_operation_display
                    ),
                )
            report = service._query_reserved(resources, query, reservation)
            return _RetrievalOperationResult(
                handoff=operation_handoff,
                report=report,
            )

    def __capture_retrieval_operation_locked(
        self,
    ) -> tuple[RetrievalHandoffSnapshot, RetrievalCapabilitySnapshot]:
        """Validate and defensively copy the current private retrieval pair."""

        handoff, publisher, service = self.__validate_retrieval_graph_locked()
        capability = publisher.snapshot()
        if (
            type(capability) is not RetrievalCapabilitySnapshot
            or _retrieval_display(capability)
            != self.__retrieval_operation_display
        ):
            raise ValueError("retrieval capability drift")
        return (
            RetrievalHandoffSnapshot(
                generation=handoff.generation,
                query_port=_CoreRetrievalQueryPort(service),
                display=_clone_retrieval_display(
                    self.__retrieval_operation_display
                ),
            ),
            capability,
        )

    def __validate_retrieval_graph_locked(
        self,
    ) -> tuple[
        RetrievalHandoffSnapshot,
        RetrievalCapabilityPublisher,
        TMRetrievalService,
    ]:
        """Validate the host-private graph without capturing capability."""

        handoff = self.__retrieval_handoff
        publisher = self.__retrieval_publisher
        service = self.__retrieval_service
        if (
            type(handoff) is not RetrievalHandoffSnapshot
            or type(handoff.query_port) is not _CoreRetrievalQueryPort
            or not handoff.query_port._is_bound_to(service)
            or handoff.display is not self.__status.retrieval
            or handoff.display != self.__retrieval_operation_display
            or handoff.generation
            != self.__retrieval_notifications.current()
            or type(publisher) is not RetrievalCapabilityPublisher
            or type(service) is not TMRetrievalService
            or cast(Any, service)._capability_publisher is not publisher
        ):
            raise ValueError("retrieval handoff drift")
        return handoff, publisher, service

    def retrieval_generation_notifications(
        self,
    ) -> RetrievalGenerationNotificationPort:
        """Return the read-only retrieval generation observer."""

        return self.__retrieval_notifications

    def status_snapshot(self) -> CapabilityDisplaySnapshot:
        """Capture the matching safe display projection."""

        with self.__lock:
            return self.__status

    def _composition_matcher_owner(
        self,
        composition_mint_identity: object,
    ) -> _MatcherValidationOwner:
        """Mint the owner object only for the application composition root."""

        if composition_mint_identity is not _COMPOSITION_MINT_IDENTITY:
            raise PermissionError(
                "matcher owner mint requires application composition"
            )
        return _MatcherValidationOwner(
            host=self,
            owner_identity=self.__matcher_owner_identity,
            checkout_identity=_APPLICATION_CHECKOUT_IDENTITY,
            factory_binding=_CORE_MATCHER_FACTORY_BINDING,
        )

    def _composition_gate_c_owner(
        self,
        composition_mint_identity: object,
        *,
        checkout_identity: _RetrievalCheckoutIdentity,
        validation_binding: _CoreRetrievalValidationBinding,
    ) -> _RetrievalGateCValidationOwner:
        """Mint the Gate C owner only for the application composition root."""

        if composition_mint_identity is not _COMPOSITION_MINT_IDENTITY:
            raise PermissionError(
                "Gate C owner mint requires application composition"
            )
        if (
            type(checkout_identity) is not _RetrievalCheckoutIdentity
            or type(validation_binding) is not _CoreRetrievalValidationBinding
            or not checkout_identity.is_current()
        ):
            raise RuntimeError("Gate C owner requires current Core bindings")
        with self.__lock:
            if (
                self.__retrieval_checkout_identity is not None
                or self.__retrieval_validation_binding is not None
            ):
                raise RuntimeError("Gate C owner has already been minted")
            self.__retrieval_checkout_identity = checkout_identity
            self.__retrieval_validation_binding = validation_binding
        return _RetrievalGateCValidationOwner(
            host=self,
            owner_identity=self.__retrieval_owner_identity,
            checkout_identity=checkout_identity,
            validation_binding=validation_binding,
        )

    def _composition_gate_d_owner(
        self,
        composition_mint_identity: object,
        *,
        attestation_root: Path | None,
    ) -> _RetrievalGateDOwner:
        """Mint the Gate D lifecycle only for application composition."""

        if composition_mint_identity is not _COMPOSITION_MINT_IDENTITY:
            raise PermissionError(
                "Gate D owner mint requires application composition"
            )
        return _RetrievalGateDOwner(
            host=self,
            owner_identity=self.__retrieval_owner_identity,
            attestation_root=attestation_root,
        )

    def _install_core_matcher(
        self,
        *,
        owner_identity: object,
        matcher: CapabilityGatedTextMatcherV1 | None,
        capability: TextMatcherCapability | None,
    ) -> MatcherHandoffSnapshot:
        """Atomically replace the handoff after Core validation."""

        if owner_identity is not self.__matcher_owner_identity:
            raise PermissionError("matcher replacement requires composition owner")
        if matcher is None:
            if capability is not None:
                raise ValueError(
                    "missing Core matcher cannot carry a capability"
                )
            display = TextMatcherDisplayState(
                state=TextMatcherState.UNAVAILABLE,
                supported_profiles=(),
                safe_reason=_MATCHER_CLOSED_REASON,
            )
            exposed_matcher: CapabilityGatedTextMatcherV1 | None = None
        else:
            if type(matcher) is not CapabilityGatedTextMatcherV1:
                raise TypeError(
                    "matcher must be constructed by the Core text-v1 factory"
                )
            if type(capability) is not TextMatcherCapability:
                raise TypeError(
                    "Core matcher capability must be TextMatcherCapability"
                )
            if capability.state is TextMatcherState.UNAVAILABLE:
                display = TextMatcherDisplayState(
                    state=TextMatcherState.UNAVAILABLE,
                    supported_profiles=(),
                    safe_reason=(
                        capability.unavailable_reason
                        or _MATCHER_CLOSED_REASON
                    ),
                )
                exposed_matcher = None
            else:
                display = TextMatcherDisplayState(
                    state=capability.state,
                    supported_profiles=capability.supported_profiles,
                    safe_reason=None,
                )
                exposed_matcher = matcher

        with self.__lock:
            generation = self.__matcher_handoff.generation + 1
            self.__matcher_notifications._validate_publish_locked(
                self.__matcher_owner_identity,
                generation,
            )
            handoff = MatcherHandoffSnapshot(
                generation=generation,
                matcher=exposed_matcher,
                display=display,
            )
            status = CapabilityDisplaySnapshot(
                matcher=handoff.display,
                retrieval=self.__retrieval_handoff.display,
            )
            old_handoff = self.__matcher_handoff
            old_status = self.__status
            old_notification_generation = (
                self.__matcher_notifications._precommit_snapshot_locked()
            )
            try:
                self.__matcher_handoff = handoff
                self.__status = status
                self.__matcher_notifications._publish_prevalidated_locked(
                    generation
                )
            except BaseException:
                self.__matcher_handoff = old_handoff
                self.__status = old_status
                self.__matcher_notifications._restore_precommit_locked(
                    old_notification_generation
                )
                raise
            return handoff

    def _install_gate_c_service(
        self,
        *,
        owner_identity: object,
        checkout_identity: _RetrievalCheckoutIdentity,
        validation_binding: _CoreRetrievalValidationBinding,
        publisher: RetrievalCapabilityPublisher,
        service: TMRetrievalService,
        capability: RetrievalCapabilitySnapshot,
        base_manifest: RetrievalCapabilityManifest,
    ) -> RetrievalHandoffSnapshot:
        """Atomically replace the full retrieval graph after Gate C."""

        if owner_identity is not self.__retrieval_owner_identity:
            raise PermissionError("Gate C replacement requires composition owner")
        if (
            checkout_identity is not self.__retrieval_checkout_identity
            or validation_binding is not self.__retrieval_validation_binding
        ):
            raise PermissionError(
                "Gate C replacement requires the loaded Core binding"
            )
        if type(publisher) is not RetrievalCapabilityPublisher:
            raise TypeError("Gate C publisher must be the Core publisher")
        if type(service) is not TMRetrievalService:
            raise TypeError("Gate C service must be TMRetrievalService")
        if type(capability) is not RetrievalCapabilitySnapshot:
            raise TypeError("Gate C capability must be a Core snapshot")
        if type(base_manifest) is not RetrievalCapabilityManifest:
            raise TypeError("Gate C base manifest must be a Core manifest")
        if cast(Any, service)._capability_publisher is not publisher:
            raise ValueError("Gate C service must retain the paired publisher")
        display = _retrieval_display(capability)
        if display.fuzzy_available:
            raise ValueError("Gate C cannot open a fuzzy execution path")
        if not (capability.context.available or capability.fuzzy_core.available):
            raise ValueError("Gate C did not validate a correctness cohort")

        with self.__retrieval_lifecycle_lock:
            with self.__lock:
                if (
                    not checkout_identity.is_current()
                    or not validation_binding.is_current()
                    or publisher.snapshot() is not capability
                ):
                    return self.__retrieval_handoff
                generation = self.__retrieval_handoff.generation + 1
                handoff = RetrievalHandoffSnapshot(
                    generation=generation,
                    query_port=_CoreRetrievalQueryPort(service),
                    display=display,
                )
                status = CapabilityDisplaySnapshot(
                    matcher=self.__matcher_handoff.display,
                    retrieval=handoff.display,
                )
                self.__retrieval_notifications._validate_publish_locked(
                    self.__retrieval_owner_identity,
                    generation,
                )
                old_publisher = self.__retrieval_publisher
                old_service = self.__retrieval_service
                old_base_manifest = self.__retrieval_base_manifest
                old_handoff = self.__retrieval_handoff
                old_operation_display = self.__retrieval_operation_display
                old_status = self.__status
                old_notification_generation = (
                    self.__retrieval_notifications
                    ._precommit_snapshot_locked()
                )
                try:
                    self.__retrieval_publisher = publisher
                    self.__retrieval_service = service
                    self.__retrieval_base_manifest = base_manifest
                    self.__retrieval_handoff = handoff
                    self.__retrieval_operation_display = (
                        _clone_retrieval_display(display)
                    )
                    self.__status = status
                    self.__retrieval_notifications._publish_prevalidated_locked(
                        generation
                    )
                except BaseException:
                    self.__retrieval_publisher = old_publisher
                    self.__retrieval_service = old_service
                    self.__retrieval_base_manifest = old_base_manifest
                    self.__retrieval_handoff = old_handoff
                    self.__retrieval_operation_display = old_operation_display
                    self.__status = old_status
                    self.__retrieval_notifications._restore_precommit_locked(
                        old_notification_generation
                    )
                    raise
                return handoff

    def _capture_gate_d_graph(
        self,
        *,
        owner_identity: object,
    ) -> _GateDGraphSnapshot | None:
        """Capture exactly one installed Gate C graph for a Gate D run."""

        if owner_identity is not self.__retrieval_owner_identity:
            raise PermissionError("Gate D capture requires composition owner")
        with self.__lock:
            manifest = self.__retrieval_base_manifest
            if manifest is None:
                return None
            publisher = self.__retrieval_publisher
            service = self.__retrieval_service
            capability = publisher.snapshot()
            if (
                cast(Any, service)._capability_publisher is not publisher
                or not capability.fuzzy_core.available
            ):
                return None
            return _GateDGraphSnapshot(
                publisher=publisher,
                service=service,
                base_manifest=manifest,
                handoff=self.__retrieval_handoff,
                capability=capability,
                publication_nonce=object(),
            )

    def _publish_gate_d_capability(
        self,
        *,
        owner_identity: object,
        graph: _GateDGraphSnapshot,
        publication: _CoreGateDPublication,
        evaluated_at_utc: datetime,
        prepare_owner_success: Callable[[], None] | None = None,
        rollback_owner_success: Callable[[], None] | None = None,
    ) -> RetrievalHandoffSnapshot | None:
        """Publish and install one still-current Gate D graph generation."""

        if owner_identity is not self.__retrieval_owner_identity:
            raise PermissionError(
                "Gate D publication requires composition owner"
            )
        if type(graph) is not _GateDGraphSnapshot:
            raise TypeError("Gate D graph capture is invalid")
        if type(publication) is not _CoreGateDPublication:
            raise TypeError("Gate D publication receipt is invalid")
        if (prepare_owner_success is None) != (rollback_owner_success is None):
            raise TypeError(
                "Gate D owner lifecycle callbacks must be provided together"
            )
        with self.__retrieval_lifecycle_lock:
            with self.__lock:
                if (
                    self.__retrieval_publisher is not graph.publisher
                    or self.__retrieval_service is not graph.service
                    or self.__retrieval_base_manifest is not graph.base_manifest
                    or self.__retrieval_handoff is not graph.handoff
                    or graph.publisher.snapshot() is not graph.capability
                ):
                    return None
                generation = graph.handoff.generation + 1
                self.__retrieval_notifications._validate_publish_locked(
                    self.__retrieval_owner_identity,
                    generation,
                )
                old_handoff = self.__retrieval_handoff
                old_operation_display = self.__retrieval_operation_display
                old_status = self.__status
                old_notification_generation = (
                    self.__retrieval_notifications
                    ._precommit_snapshot_locked()
                )
                prepared_handoff = graph.handoff

                def prepare_publication(
                    capability: RetrievalCapabilitySnapshot,
                ) -> _GateDExecutionResult:
                    nonlocal prepared_handoff
                    if (
                        capability.context != graph.capability.context
                        or capability.fuzzy_core
                        != graph.capability.fuzzy_core
                    ):
                        raise RuntimeError(
                            "Gate D candidate changed Gate C authority"
                        )
                    display = _retrieval_display(capability)
                    handoff = RetrievalHandoffSnapshot(
                        generation=generation,
                        query_port=graph.handoff.query_port,
                        display=display,
                    )
                    status = CapabilityDisplaySnapshot(
                        matcher=self.__matcher_handoff.display,
                        retrieval=display,
                    )
                    prepared = _GateDExecutionResult(
                        snapshot=capability,
                        handoff=handoff,
                        status=status,
                    )
                    try:
                        self.__retrieval_handoff = handoff
                        self.__retrieval_operation_display = (
                            _clone_retrieval_display(display)
                        )
                        self.__status = status
                        self.__retrieval_notifications._publish_prevalidated_locked(
                            generation
                        )
                        if prepare_owner_success is not None:
                            prepare_owner_success()
                    except BaseException:
                        self.__retrieval_handoff = old_handoff
                        self.__retrieval_operation_display = (
                            old_operation_display
                        )
                        self.__status = old_status
                        self.__retrieval_notifications._restore_precommit_locked(
                            old_notification_generation
                        )
                        if rollback_owner_success is not None:
                            rollback_owner_success()
                        raise
                    prepared_handoff = handoff
                    return prepared

                publication.publish(
                    publication_owner_identity=owner_identity,
                    publication_graph_nonce=graph.publication_nonce,
                    base_manifest=graph.base_manifest,
                    publisher=graph.publisher,
                    evaluated_at_utc=evaluated_at_utc,
                    prepare_publication=prepare_publication,
                )
                return prepared_handoff


@final
class _MatcherValidationOwner:
    """Narrow object capability retained by the composition root only."""

    __slots__ = (
        "__checkout_identity",
        "__factory_binding",
        "__host",
        "__owner_identity",
    )

    def __init__(
        self,
        *,
        host: CapabilityHost,
        owner_identity: object,
        checkout_identity: _ApplicationCheckoutIdentity,
        factory_binding: _CoreMatcherFactoryBinding,
    ) -> None:
        if type(host) is not CapabilityHost:
            raise TypeError("matcher owner requires CapabilityHost")
        if checkout_identity is not _APPLICATION_CHECKOUT_IDENTITY:
            raise PermissionError(
                "matcher owner requires the loaded application checkout"
            )
        if factory_binding is not _CORE_MATCHER_FACTORY_BINDING:
            raise PermissionError(
                "matcher owner requires the loaded Core factory binding"
            )
        self.__host = host
        self.__owner_identity = owner_identity
        self.__checkout_identity = checkout_identity
        self.__factory_binding = factory_binding

    def validate_basic(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> MatcherHandoffSnapshot:
        return self.__validate(
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
            evaluated_at_utc=evaluated_at_utc,
            include_full=False,
        )

    def validate_text_v1(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> MatcherHandoffSnapshot:
        return self.__validate(
            generated_at_utc=generated_at_utc,
            valid_until_utc=valid_until_utc,
            evaluated_at_utc=evaluated_at_utc,
            include_full=True,
        )

    def __validate(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
        include_full: bool,
    ) -> MatcherHandoffSnapshot:
        if (
            not self.__checkout_identity.is_current()
            or not self.__factory_binding.is_current()
        ):
            return self.__publish_unavailable()
        try:
            matcher = self.__factory_binding.invoke(
                repository_root=self.__checkout_identity.root.path,
                generated_at_utc=generated_at_utc,
                valid_until_utc=valid_until_utc,
                evaluated_at_utc=evaluated_at_utc,
                include_full=include_full,
            )
        except (OSError, ValueError):
            return self.__publish_unavailable()
        if (
            matcher is None
            or not self.__checkout_identity.is_current()
            or not self.__factory_binding.is_current()
        ):
            return self.__publish_unavailable()
        if type(matcher) is not CapabilityGatedTextMatcherV1:
            raise TypeError(
                "Core validated matcher factory returned an invalid type"
            )
        capability = matcher.capability()
        return self.__host._install_core_matcher(
            owner_identity=self.__owner_identity,
            matcher=matcher,
            capability=capability,
        )

    def __publish_unavailable(self) -> MatcherHandoffSnapshot:
        return self.__host._install_core_matcher(
            owner_identity=self.__owner_identity,
            matcher=None,
            capability=None,
        )

    def _is_bound_to(self, host: CapabilityHost) -> bool:
        return self.__host is host


@final
class _RetrievalGateCValidationOwner:
    """Narrow owner of current-checkout Gate C recomputation and swap."""

    __slots__ = (
        "__checkout_identity",
        "__host",
        "__owner_identity",
        "__validation_binding",
        "__validation_lock",
    )

    def __init__(
        self,
        *,
        host: CapabilityHost,
        owner_identity: object,
        checkout_identity: _RetrievalCheckoutIdentity,
        validation_binding: _CoreRetrievalValidationBinding,
    ) -> None:
        if type(host) is not CapabilityHost:
            raise TypeError("Gate C owner requires CapabilityHost")
        if (
            type(checkout_identity) is not _RetrievalCheckoutIdentity
            or type(validation_binding) is not _CoreRetrievalValidationBinding
        ):
            raise PermissionError("Gate C owner requires Core bindings")
        self.__host = host
        self.__owner_identity = owner_identity
        self.__checkout_identity = checkout_identity
        self.__validation_binding = validation_binding
        self.__validation_lock = Lock()

    def validate_gate_c(
        self,
        *,
        generated_at_utc: datetime,
        valid_until_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> RetrievalHandoffSnapshot:
        """Recompute Gate C and swap only one fully validated Core graph."""

        with self.__validation_lock:
            current = self.__host.retrieval_snapshot()
            if (
                not self.__checkout_identity.is_current()
                or not self.__validation_binding.is_current()
            ):
                return current
            try:
                release = self.__validation_binding.recompute(
                    repository_root=self.__checkout_identity.root.path,
                    generated_at_utc=generated_at_utc,
                    valid_until_utc=valid_until_utc,
                )
            except (OSError, ValueError):
                return current
            if release is None:
                return current
            try:
                graph = self.__validation_binding.compose_service(
                    release,
                    evaluated_at_utc=evaluated_at_utc,
                )
            except (OSError, RuntimeError, ValueError):
                return current
            if graph is None:
                return current
            publisher, service, capability, base_manifest = graph
            if (
                not self.__checkout_identity.is_current()
                or not self.__validation_binding.is_current()
            ):
                return current
            try:
                return self.__host._install_gate_c_service(
                    owner_identity=self.__owner_identity,
                    checkout_identity=self.__checkout_identity,
                    validation_binding=self.__validation_binding,
                    publisher=publisher,
                    service=service,
                    capability=capability,
                    base_manifest=base_manifest,
                )
            except (OSError, RuntimeError, ValueError):
                return current

    def _is_bound_to(self, host: CapabilityHost) -> bool:
        return self.__host is host


@final
class _RetrievalGateDOwner:
    """Composition-private, process-local asynchronous Gate D owner."""

    __slots__ = (
        "__attestation_root",
        "__condition",
        "__execute",
        "__host",
        "__owner_identity",
        "__programmer_error",
        "__retained_roots",
        "__restore",
        "__status",
        "__thread",
    )

    def __init__(
        self,
        *,
        host: CapabilityHost,
        owner_identity: object,
        attestation_root: Path | None,
    ) -> None:
        if type(host) is not CapabilityHost:
            raise TypeError("Gate D owner requires CapabilityHost")
        self.__host = host
        self.__owner_identity = owner_identity
        if attestation_root is not None and (
            not isinstance(attestation_root, Path)
            or not attestation_root.is_absolute()
        ):
            raise ValueError("Gate D attestation root must be absolute")
        self.__attestation_root = attestation_root
        self.__execute = _REAL_GATE_D_EXECUTE
        self.__restore = _REAL_GATE_D_RESTORE
        self.__condition = Condition(Lock())
        self.__status = GateDRunStatus(
            epoch=0,
            state=GateDRunState.IDLE,
            safe_code=None,
        )
        self.__thread: Thread | None = None
        self.__programmer_error: Exception | None = None
        self.__retained_roots: list[Path] = []

    def start_gate_d(
        self,
        *,
        evaluated_at_utc: datetime,
    ) -> GateDRunStatus:
        if (
            type(evaluated_at_utc) is not datetime
            or evaluated_at_utc.tzinfo is None
            or evaluated_at_utc.utcoffset()
            != timezone.utc.utcoffset(evaluated_at_utc)
        ):
            raise ValueError(
                "Gate D evaluated_at_utc must be timezone-aware UTC"
            )
        graph = self.__host._capture_gate_d_graph(
            owner_identity=self.__owner_identity,
        )
        with self.__condition:
            if self.__status.state is GateDRunState.RUNNING:
                return self.__status
            if graph is None:
                failed = GateDRunStatus(
                    epoch=self.__status.epoch,
                    state=GateDRunState.FAILED,
                    safe_code="GATE_D.GATE_C_REQUIRED",
                )
                self.__status = failed
                return failed
            started = GateDRunStatus(
                epoch=self.__status.epoch + 1,
                state=GateDRunState.RUNNING,
                safe_code=None,
            )
            self.__status = started
            self.__programmer_error = None
            thread = Thread(
                target=self.__run,
                kwargs={
                    "epoch": started.epoch,
                    "graph": graph,
                    "evaluated_at_utc": evaluated_at_utc,
                },
                name=f"LocalCAT-GateD-{started.epoch}",
                daemon=True,
            )
            self.__thread = thread
            thread.start()
            return started

    def status(self) -> GateDRunStatus:
        with self.__condition:
            return self.__status

    def restore_gate_d(
        self,
        *,
        evaluated_at_utc: datetime,
    ) -> GateDRunStatus:
        """Restore one compatible device qualification without rerunning 100k."""

        if (
            type(evaluated_at_utc) is not datetime
            or evaluated_at_utc.tzinfo is None
            or evaluated_at_utc.utcoffset()
            != timezone.utc.utcoffset(evaluated_at_utc)
        ):
            raise ValueError(
                "Gate D evaluated_at_utc must be timezone-aware UTC"
            )
        graph = self.__host._capture_gate_d_graph(
            owner_identity=self.__owner_identity,
        )
        with self.__condition:
            if self.__status.state is GateDRunState.RUNNING:
                return self.__status
            epoch = self.__status.epoch + 1
            self.__status = GateDRunStatus(
                epoch=epoch,
                state=GateDRunState.RUNNING,
                safe_code=None,
            )
            self.__programmer_error = None
        try:
            if graph is None:
                raise _GateDOperationalError("GATE_D.GATE_C_REQUIRED")
            state_root = self.__attestation_root
            if state_root is None:
                raise _GateDOperationalError(
                    "GATE_D.REVALIDATION_REQUIRED"
                )
            publication = self.__restore(
                contract_path=_GATE_D_CONTRACT_ANCHOR.path,
                state_root=state_root,
                base_manifest=graph.base_manifest,
                publication_owner_identity=self.__owner_identity,
                publication_graph_nonce=graph.publication_nonce,
            )
            if type(publication) is not _CoreGateDPublication:
                raise TypeError("Gate D restoration returned an invalid result")
            self.__publish_publication(
                epoch=epoch,
                graph=graph,
                publication=publication,
                evaluated_at_utc=evaluated_at_utc,
            )
        except _GateDOperationalError as error:
            failed = GateDRunStatus(
                epoch=epoch,
                state=GateDRunState.FAILED,
                safe_code=error.error_code,
            )
            self.__finish(failed)
            return failed
        except OSError:
            failed = GateDRunStatus(
                epoch=epoch,
                state=GateDRunState.FAILED,
                safe_code="GATE_D.ATTESTATION_UNAVAILABLE",
            )
            self.__finish(failed)
            return failed
        except Exception as error:
            self.__finish(
                GateDRunStatus(
                    epoch=epoch,
                    state=GateDRunState.FAILED,
                    safe_code="GATE_D.PROGRAMMER_ERROR",
                ),
                programmer_error=error,
            )
            raise
        return self.status()

    def wait(self, timeout: float | None = None) -> GateDRunStatus:
        if timeout is not None:
            if type(timeout) not in (int, float):
                raise TypeError("Gate D timeout must be numeric")
            numeric_timeout = float(timeout)
            if not math.isfinite(numeric_timeout) or numeric_timeout < 0.0:
                raise ValueError(
                    "Gate D timeout must be finite and non-negative"
                )
        else:
            numeric_timeout = None
        with self.__condition:
            self.__condition.wait_for(
                lambda: self.__status.state is not GateDRunState.RUNNING,
                timeout=numeric_timeout,
            )
            programmer_error = self.__programmer_error
            status = self.__status
        if programmer_error is not None:
            raise programmer_error
        return status

    def __run(
        self,
        *,
        epoch: int,
        graph: _GateDGraphSnapshot,
        evaluated_at_utc: datetime,
    ) -> None:
        try:
            raw_root = tempfile.mkdtemp(
                prefix="localcat-gate-d-",
            )
            work_root = Path(raw_root).resolve(strict=True)
            os.chmod(work_root, 0o700)
            mode = work_root.lstat().st_mode
            if (
                work_root.resolve(strict=True) != work_root
                or not stat.S_ISDIR(mode)
                or stat.S_IMODE(mode) != 0o700
            ):
                raise _GateDOperationalError(
                    "GATE_D.WORK_ROOT_INVALID"
                )
            evidence_path = work_root / "benchmark_tm_evidence.json"
            if evidence_path.exists():
                raise _GateDOperationalError(
                    "GATE_D.EVIDENCE_PATH_EXISTS"
                )
            self.__retained_roots.append(work_root)
            publication = self.__execute(
                contract_path=_GATE_D_CONTRACT_ANCHOR.path,
                work_root=work_root,
                evidence_path=evidence_path,
                publication_owner_identity=self.__owner_identity,
                publication_graph_nonce=graph.publication_nonce,
            )
            if type(publication) is not _CoreGateDPublication:
                raise TypeError("Gate D execution returned an invalid result")
            if self.__attestation_root is not None:
                publication.persist_attestation(
                    contract_path=_GATE_D_CONTRACT_ANCHOR.path,
                    state_root=self.__attestation_root,
                    base_manifest=graph.base_manifest,
                    issued_at_utc=evaluated_at_utc,
                )
            self.__publish_publication(
                epoch=epoch,
                graph=graph,
                publication=publication,
                evaluated_at_utc=evaluated_at_utc,
            )
        except _GateDOperationalError as error:
            self.__finish(
                GateDRunStatus(
                    epoch=epoch,
                    state=GateDRunState.FAILED,
                    safe_code=error.error_code,
                )
            )
            return
        except OSError:
            self.__finish(
                GateDRunStatus(
                    epoch=epoch,
                    state=GateDRunState.FAILED,
                    safe_code="GATE_D.WORK_ROOT_UNAVAILABLE",
                )
            )
            return
        except Exception as error:
            self.__finish(
                GateDRunStatus(
                    epoch=epoch,
                    state=GateDRunState.FAILED,
                    safe_code="GATE_D.PROGRAMMER_ERROR",
                ),
                programmer_error=error,
            )
            return
        return

    def __publish_publication(
        self,
        *,
        epoch: int,
        graph: _GateDGraphSnapshot,
        publication: _CoreGateDPublication,
        evaluated_at_utc: datetime,
    ) -> None:
        succeeded = GateDRunStatus(
            epoch=epoch,
            state=GateDRunState.SUCCEEDED,
            safe_code=None,
        )
        with self.__condition:
            old_status = self.__status
            old_programmer_error = self.__programmer_error

            def prepare_owner_success() -> None:
                self.__status = succeeded
                self.__programmer_error = None
                self.__condition.notify_all()

            def rollback_owner_success() -> None:
                self.__status = old_status
                self.__programmer_error = old_programmer_error

            installed = self.__host._publish_gate_d_capability(
                owner_identity=self.__owner_identity,
                graph=graph,
                publication=publication,
                evaluated_at_utc=evaluated_at_utc,
                prepare_owner_success=prepare_owner_success,
                rollback_owner_success=rollback_owner_success,
            )
            if installed is None:
                raise _GateDOperationalError("GATE_D.GATE_C_CHANGED")

    def __finish(
        self,
        status: GateDRunStatus,
        *,
        programmer_error: Exception | None = None,
    ) -> None:
        with self.__condition:
            if (
                self.__status.state is not GateDRunState.RUNNING
                or self.__status.epoch != status.epoch
            ):
                return
            self.__status = status
            self.__programmer_error = programmer_error
            self.__condition.notify_all()

    def _is_bound_to(self, host: CapabilityHost) -> bool:
        return self.__host is host


@dataclass(frozen=True, slots=True)
class CapabilityHostComposition:
    """Split the runtime read port from its composition-owner validation port."""

    host: CapabilityHost
    matcher_validation_owner: MatcherValidationOwnerPort
    retrieval_gate_c_validation_owner: (
        RetrievalGateCValidationOwnerPort | None
    ) = None
    retrieval_gate_d_owner: RetrievalGateDOwnerPort | None = None

    def __post_init__(self) -> None:
        if type(self.host) is not CapabilityHost:
            raise TypeError("capability composition host must be CapabilityHost")
        if type(self.matcher_validation_owner) is not _MatcherValidationOwner:
            raise TypeError(
                "matcher validation owner must be host-owned"
            )
        if not self.matcher_validation_owner._is_bound_to(self.host):
            raise ValueError(
                "matcher validation owner must be bound to this host"
            )
        if type(self.retrieval_gate_c_validation_owner) is not (
            _RetrievalGateCValidationOwner
        ):
            raise TypeError(
                "Gate C validation owner must be host-owned"
            )
        if not self.retrieval_gate_c_validation_owner._is_bound_to(self.host):
            raise ValueError(
                "Gate C validation owner must be bound to this host"
            )
        if type(self.retrieval_gate_d_owner) is not _RetrievalGateDOwner:
            raise TypeError("Gate D owner must be host-owned")
        if not self.retrieval_gate_d_owner._is_bound_to(self.host):
            raise ValueError("Gate D owner must be bound to this host")


def compose_capability_host(
    *,
    evaluated_at_utc: datetime,
    gate_d_attestation_root: Path | None = None,
) -> CapabilityHostComposition:
    """Create the application-owned host and its private validation control."""

    retrieval_binding, retrieval_checkout = (
        _load_retrieval_validation_binding()
    )
    host = CapabilityHost(evaluated_at_utc=evaluated_at_utc)
    return CapabilityHostComposition(
        host=host,
        matcher_validation_owner=host._composition_matcher_owner(
            _COMPOSITION_MINT_IDENTITY,
        ),
        retrieval_gate_c_validation_owner=host._composition_gate_c_owner(
            _COMPOSITION_MINT_IDENTITY,
            checkout_identity=retrieval_checkout,
            validation_binding=retrieval_binding,
        ),
        retrieval_gate_d_owner=host._composition_gate_d_owner(
            _COMPOSITION_MINT_IDENTITY,
            attestation_root=gate_d_attestation_root,
        ),
    )


__all__ = [
    "CapabilityHostComposition",
    "CapabilityDisplaySnapshot",
    "CapabilityHost",
    "GateDRunState",
    "GateDRunStatus",
    "MatcherGenerationNotificationPort",
    "MatcherHandoffSnapshot",
    "MatcherValidationOwnerPort",
    "RetrievalGateCValidationOwnerPort",
    "RetrievalGateDOwnerPort",
    "RetrievalGenerationNotificationPort",
    "RetrievalHandoffSnapshot",
    "RetrievalQueryPort",
    "compose_capability_host",
]
