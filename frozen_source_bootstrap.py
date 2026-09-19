"""Native-only retained source producer, executed after E10 by the custom entry.

No filesystem loader, source checkout, bytecode cache or Python factory can
create this process's producer. The native builtin owns its unique identity.
"""
import sys
import _thread
import _frozen_importlib as _importlib
import _imp
# Compiled into this EXE; the accompanying pyi describes its C-only interface.
import _localcat_frozen_bootstrap as _native  # pyright: ignore[reportMissingModuleSource]


_raw: _native.FrozenBootstrapAuthority = globals()["_authority"]
_argv: tuple[str, ...] = globals()["_entry_argv"]
if type(_raw) is not _native.FrozenBootstrapAuthority or type(_argv) is not tuple:
    raise RuntimeError("frozen producer requires the native E10 handoff")
if _argv == ():
    _mode = "product"
elif len(_argv) == 2 and _argv[0] == "--localcat-tm-worker" and _argv[1] in ("migration", "query"):
    _mode = _argv[1]
else:
    _raw.close()
    raise RuntimeError("frozen entry arguments are not a supported product or worker mode")

# Dispatch selection precedes every project, Core, platform and Qt import.
_initial_thread = _thread.get_ident()
_bootstrap_proof = _raw.module_reproof("bootstrap")
_executable = sys.executable
_bundle_root = sys.executable.rsplit("\\", 1)[0]
if not _bundle_root or _bundle_root == sys.executable:
    raise RuntimeError("native executable locator is unavailable")
_catalog_globals: "dict[str, Any]" = {"__builtins__": {}}
exec(compile(_raw.read_verified("catalog"), "<localcat-retained:catalog>", "exec"), _catalog_globals)
_records = _catalog_globals.get("ENTRIES")
_app_relative = _catalog_globals.get("APPLICATION_ROOT")
if type(_records) is not tuple or type(_app_relative) is not str or _app_relative != "_internal/app":
    raise RuntimeError("invalid native-bound source catalog")


def _canonical(value):
    return (type(value) is str and value and "\\" not in value and ":" not in value
            and all(part not in ("", ".", "..") for part in value.split("/")))


_by_module = {}
_by_relative = {}
_entry_records = {}
for _record in _records:
    if type(_record) is not tuple or len(_record) != 6:
        raise RuntimeError("invalid retained catalog record")
    _id, _physical, _logical, _name, _package, _kind = _record
    if (type(_id) is not str or not _canonical(_physical) or type(_package) is not bool
            or _kind not in ("source", "extension", "fixture") or _id in _entry_records):
        raise RuntimeError("ambiguous retained catalog record")
    if _logical is not None:
        if not _canonical(_logical) or _physical != _app_relative + "/" + _logical or _logical in _by_relative:
            raise RuntimeError("ambiguous retained application input")
        _by_relative[_logical] = _record
    if _name is not None:
        if type(_name) is not str or not all(part.isidentifier() for part in _name.split(".")) or _name in _by_module:
            raise RuntimeError("ambiguous retained module name")
        if _kind == "fixture":
            raise RuntimeError("fixture cannot be an executable module")
        _by_module[_name] = _record
    _entry_records[_id] = _record

_executed = {}
_module_type = type(sys)
_function_type = type(_canonical)


def _anchors(module):
    result = []
    visited = set()
    def visit(path, value):
        if type(value) in (staticmethod, classmethod):
            result.append((path, value))
            visit(path + ("__func__",), value.__func__)
        elif type(value) is property:
            result.append((path, value))
            for member in ("fget", "fset", "fdel"):
                function = getattr(value, member)
                if function is not None:
                    visit(path + (member,), function)
        elif type(value) is _function_type and value.__module__ == module.__name__:
            # Code objects and their nested co_consts are immutable; exact root
            # identity binds the entire compiled nested-code graph.
            result.append((path, value, value.__code__))
        elif isinstance(value, type) and value.__module__ == module.__name__:
            result.append((path, value))
            if value not in visited:
                visited.add(value)
                for member, method in vars(value).items():
                    visit(path + (member,), method)
    for name, value in vars(module).items():
        visit((name,), value)
    return tuple(result)


def _native_call(operation):
    producer = globals().get("_producer")
    if producer is not None:
        return producer._invoke(operation)
    if _thread.get_ident() != _initial_thread:
        raise RuntimeError("pre-producer import requires the native owner")
    return operation()


class _RetainedLoader:
    def __init__(self, record):
        self._record = record
        self._origin = _bundle_root + "\\" + record[1].replace("/", "\\")

    def create_module(self, spec):
        if self._record[5] == "extension":
            proof = _native_call(lambda: _raw.module_reproof(self._record[0]))
            if proof[0] is not True or proof[1] != 1:
                raise ImportError("extension has no native mapped-image proof")
            return _imp.create_dynamic(spec)
        return None

    def exec_module(self, module):
        record = self._record
        proof = _native_call(lambda: _raw.module_reproof(record[0]))
        if record[5] == "extension":
            _imp.exec_dynamic(module)
            if _native_call(lambda: _raw.module_reproof(record[0])) != proof:
                raise ImportError("extension identity changed during execution")
            content = None
        else:
            if proof[1] not in (2, 4):
                raise ImportError("catalog does not designate retained source")
            content = _native_call(lambda: _raw.read_verified(record[0]))
            code = compile(content, self._origin, "exec", dont_inherit=True)
            exec(code, module.__dict__)
            if _native_call(lambda: _raw.module_reproof(record[0])) != proof:
                raise ImportError("retained source identity changed during execution")
        _executed[record[3]] = (module, module.__spec__, self, self._origin, content, proof, _anchors(module))

    def get_filename(self, fullname):
        if fullname != self._record[3]:
            raise ImportError("loader module mismatch")
        return self._origin

    def get_source(self, fullname):
        raise ImportError("retained source is available only through a proof window")


class _RetainedFinder:
    def find_spec(self, fullname, path=None, target=None):
        record = _by_module.get(fullname)
        if record is None:
            # Only the manifest-bound interpreter's builtins/frozen modules are
            # allowed outside this catalog. No PathFinder ever receives a miss.
            if _imp.is_builtin(fullname) or _imp.is_frozen(fullname):
                return None
            raise ModuleNotFoundError("module is outside the retained catalog: " + fullname, name=fullname)
        loader: Any = _RetainedLoader(record)
        spec = _importlib.ModuleSpec(fullname, loader, origin=loader._origin, is_package=record[4])
        spec.has_location = True
        if record[4]:
            spec.submodule_search_locations = []
        return spec


sys.path[:] = []
sys.path_hooks[:] = []
sys.path_importer_cache.clear()
sys.meta_path[:] = [_RetainedFinder(), _importlib.BuiltinImporter, _importlib.FrozenImporter]
setattr(sys, "frozen", True)
sys.argv = [sys.executable, *_argv]
# The native module remains the same object; this is a name, not a second load.
sys.modules["frozen_source_bootstrap"] = sys.modules[__name__]
__name__ = "frozen_source_bootstrap"

from contextlib import contextmanager
from pathlib import Path
from threading import RLock, get_ident
from typing import Any


class _InputLease:
    """One Core owner's borrow. Closing this object never closes the producer."""
    def __init__(self, producer):
        self._producer = producer
        self._closed = False
        self._window = None
        self._seen = {}

    def reprove(self):
        if self._closed:
            raise RuntimeError("frozen input borrow is closed")
        self._producer._invoke(lambda: _raw.module_reproof("bootstrap"), self._window)

    @contextmanager
    def proof_window(self):
        if self._closed or self._window is not None:
            raise RuntimeError("frozen borrow window is unavailable")
        with self._producer._proof_window() as window:
            self._window = window
            self._seen = {}
            try:
                self.reprove()
                yield
                # Fresh retained reproof happens before Core prepares its pure
                # memory publication plan. Cached bytes never satisfy terminal.
                for entry_id, proof in tuple(self._seen.items()):
                    if self._producer._invoke(lambda: _raw.module_reproof(entry_id), window) != proof:
                        raise RuntimeError("frozen input terminal identity changed")
                self.reprove()
            finally:
                self._window = None
                self._seen = {}

    def read_bytes(self, relative):
        if self._closed or self._window is None:
            raise RuntimeError("frozen read requires its current borrow window")
        record = _by_relative.get(relative)
        if record is None:
            raise ValueError("input is outside the retained application closure")
        def read():
            proof = _raw.module_reproof(record[0])
            content = _raw.read_verified(record[0])
            if _raw.module_reproof(record[0]) != proof:
                raise RuntimeError("frozen input changed while reading")
            return content, proof
        content, proof = self._producer._invoke(read, self._window)
        if record[0] in self._seen and self._seen[record[0]] != proof:
            raise RuntimeError("frozen input changed in a proof window")
        self._seen[record[0]] = proof
        return content

    def close(self):
        self._closed = True


class _FrozenProducer:
    __slots__ = ("_closed", "_native_closed", "_scheduler", "_lock", "_active", "_root", "_consumer_thread", "_current_window")

    def __init__(self):
        self._closed = False
        self._native_closed = False
        self._scheduler = None
        self._lock = RLock()
        self._active = None
        self._consumer_thread = None
        self._current_window = None
        self._root = Path(_bundle_root + "\\_internal\\app")

    @property
    def root_path(self):
        self._require_live()
        return self._root

    def _require_live(self):
        if self._closed or not _native.is_producer(self):
            raise RuntimeError("frozen producer is revoked or foreign")

    def _owner_call(self, operation):
        if get_ident() != _initial_thread:
            raise RuntimeError("native proof was not dispatched to its owner")
        try:
            self._require_live()
            return operation()
        except BaseException:
            self.close()
            raise

    def _invoke(self, operation, window=None):
        self._require_live()
        with self._lock:
            if self._active is not None:
                if get_ident() != self._consumer_thread:
                    raise RuntimeError("frozen proof belongs to another consumer")
                if window is None:
                    window = self._current_window
        if get_ident() == _initial_thread:
            return self._owner_call(operation)
        scheduler = self._scheduler
        if scheduler is None:
            raise RuntimeError("background frozen proof requires an owner scheduler")
        if window is not None:
            return scheduler.call(window, lambda: self._owner_call(operation))
        with scheduler.window() as temporary:
            return scheduler.call(temporary, lambda: self._owner_call(operation))

    @contextmanager
    def _proof_window(self):
        self._require_live()
        token = object()
        with self._lock:
            if self._active is not None:
                raise RuntimeError("frozen proof windows cannot overlap")
            self._active = token
            self._consumer_thread = get_ident()
        try:
            if self._scheduler is None:
                if get_ident() != _initial_thread:
                    raise RuntimeError("frozen proof requires its native owner")
                self._current_window = token
                yield token
            else:
                with self._scheduler.window() as window:
                    self._current_window = window
                    yield window
            self._require_live()
        finally:
            with self._lock:
                if self._active is token:
                    self._active = None
                    self._consumer_thread = None
                    self._current_window = None

    def _borrow_core_inputs(self):
        self._require_live()
        return _InputLease(self)

    def _resource_names(self, prefix):
        self._require_live()
        self._invoke(lambda: _raw.module_reproof("catalog"))
        return tuple(sorted(name for name, record in _by_relative.items()
                            if name.startswith(prefix) and record[5] == "fixture"))

    def _qt_plugin_root(self):
        self._require_live()
        entries = tuple(record for record in _entry_records.values()
                        if record[1] == "_internal/packages/PySide6/plugins/platforms/qwindows.dll")
        if len(entries) != 1 or self._invoke(lambda: _raw.module_reproof(entries[0][0]))[0] is not True:
            raise RuntimeError("Qt platform plugin is outside the native closure")
        return Path(_bundle_root + "/" + entries[0][1]).parent.parent

    def _install_owner_scheduler(self, scheduler):
        if get_ident() != _initial_thread:
            raise RuntimeError("scheduler installation requires the initial native owner")
        self._require_live()
        with self._lock:
            if self._scheduler is not None or self._active is not None:
                raise RuntimeError("owner scheduler can be installed once outside a proof window")
            for name in ("window", "call", "close", "require_worker_wait_allowed"):
                if not callable(getattr(scheduler, name, None)):
                    raise TypeError("invalid owner scheduling port")
            self._scheduler = scheduler

    def require_worker_wait_allowed(self):
        self._require_live()
        if self._scheduler is not None and get_ident() == _initial_thread:
            self.close()
            raise RuntimeError("owner thread cannot join a worker requiring owner proofs")

    def _reprove_executed_module(self, module, relative, content):
        self._require_live()
        record = _by_relative.get(relative)
        if record is None or record[3] is None:
            raise RuntimeError("module does not have a retained source record")
        observed = _executed.get(record[3])
        if observed is None or type(module) is not _module_type:
            raise RuntimeError("module was not executed by the retained loader")
        expected, spec, loader, origin, exact, proof, anchors = observed
        if (module is not expected or sys.modules.get(record[3]) is not module
                or module.__spec__ is not spec or module.__loader__ is not loader
                or spec.loader is not loader or spec.origin != origin or module.__file__ != origin
                or content != exact or _anchors(module) != anchors):
            raise RuntimeError("executed module/spec/loader/code anchor changed")
        if self._invoke(lambda: _raw.module_reproof(record[0])) != proof:
            raise RuntimeError("executed module lost its retained native proof")

    def close(self):
        with self._lock:
            self._closed = True
        if get_ident() == _initial_thread and not self._native_closed:
            self._native_closed = True
            _raw.close()
        # A background cancellation revokes immediately. The initial entry's
        # finally drains native release without queueing into a closed scheduler.


_producer = _FrozenProducer()
if _raw.bind_producer(_producer) is not True or not _native.is_producer(_producer):
    raise RuntimeError("native producer registration failed")


def _run_selected_entry():
    if _mode == "product":
        from frozen_product_entry import run_product
        return run_product(_producer)
    from frozen_worker_entry import serve_selected_worker
    return serve_selected_worker(_mode, _producer)


try:
    _result = _run_selected_entry()
    if _result != 0:
        raise RuntimeError("frozen entry failed")
finally:
    _producer.close()
