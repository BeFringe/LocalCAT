"""Exact-byte bootstrap for the minimal Windows frozen-entry spike."""

_native = __import__("_localcat_frozen_bootstrap")
# E10 supplies the already-taken opaque object before this first source body.
if type(_authority) is not _native.FrozenBootstrapAuthority:
    raise RuntimeError("FROZEN_ENTRY.HANDOFF_MISSING")
_bootstrap_attestation = _authority.module_reproof("bootstrap")

# Spike-only assertions, not a product startup prerequisite. The first take
# remains in native localcat_bootstrap_execute before any source instruction.
_handoff_checks = []
_handoff_selftest = {
    "schema": "localcat.w3.handoff-selftest.v1",
    "scope": "spike-only",
    "state": "RUNNING",
    "checks": (),
    "threads": None,
    "excluded": ("early-take", "wrong-interpreter", "cross-process-pickle"),
}


def _expect_rejection(label, operation, error_type, message=None):
    try:
        operation()
    except BaseException as error:
        if type(error) is not error_type or (message is not None and str(error) != message):
            raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_WRONG_REJECTION:" + label) from None
        return (label, error_type.__name__) + (() if message is None else (message,))
    raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_UNEXPECTED_SUCCESS:" + label)


def _probe_worker(authority, completion, results, get_ident):
    checks = []
    try:
        worker_ident = get_ident()
        for label, operation in (
            ("worker-read", lambda: authority.read_verified("fixture")),
            ("worker-reproof", lambda: authority.module_reproof("fixture")),
            ("worker-close", authority.close),
        ):
            checks.append(_expect_rejection(label, operation, RuntimeError,
                                            "FROZEN_ENTRY.AUTHORITY_UNAVAILABLE"))
        results.append(("COMPLETE", tuple(checks), worker_ident))
    except BaseException as error:
        # Never lose a worker failure as an unraisable thread exception.
        results.append(("ERROR", type(error).__name__, str(error)))
    finally:
        completion.release()


def _check_worker_rejections(authority):
    # _thread is compiled into the pinned interpreter; no threading/copy/pickle
    # imports or filesystem-based stdlib dependency is introduced by the spike.
    thread = __import__("_thread")
    owner_ident = thread.get_ident()
    completion = thread.allocate_lock()
    if not completion.acquire(False):
        raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_LOCK_UNAVAILABLE")
    results = []
    started = False
    acquired = False
    try:
        created_ident = thread.start_new_thread(_probe_worker, (authority, completion, results, thread.get_ident))
        started = True
        acquired = completion.acquire(timeout=5.0)
        if not acquired:
            raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_WORKER_TIMEOUT")
        if len(results) != 1 or results[0][0] != "COMPLETE":
            raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_WORKER_FAILED")
        worker_ident = results[0][2]
        if worker_ident == owner_ident or worker_ident != created_ident:
            raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_THREAD_IDENTITY")
        return results[0][1], {"owner": owner_ident, "started": created_ident, "worker": worker_ident}
    finally:
        # After a timeout the still-running worker owns the pending release.
        # Releasing here would race/double-release its finally block. A start
        # failure has no worker; a completed wait gives this thread the lock.
        if acquired or not started:
            completion.release()


_authority_type = type(_authority)
for _label, _operation, _exception, _message in (
    ("duplicate-take", _native.take_attestation, RuntimeError, "FROZEN_ENTRY.HANDOFF_UNAVAILABLE"),
    ("construct", _authority_type, RuntimeError, "FROZEN_ENTRY.AUTHORITY_NOT_CONSTRUCTIBLE"),
    ("direct-new", lambda: _authority_type.__new__(_authority_type), RuntimeError,
     "FROZEN_ENTRY.AUTHORITY_NOT_CONSTRUCTIBLE"),
    ("base-construct", lambda: object.__new__(_authority_type), TypeError, None),
    ("subclass", lambda: type("ForgedAuthority", (_authority_type,), {}), TypeError, None),
    ("instance-write", lambda: setattr(_authority, "read_verified", None), AttributeError, None),
    ("instance-type-write", lambda: setattr(_authority, "__class__", object), TypeError, None),
    ("type-write", lambda: setattr(_authority_type, "read_verified", None), TypeError, None),
    ("copy", _authority.__copy__, RuntimeError, "FROZEN_ENTRY.AUTHORITY_NOT_SERIALIZABLE"),
    ("deepcopy", lambda: _authority.__deepcopy__({}), RuntimeError, "FROZEN_ENTRY.AUTHORITY_NOT_SERIALIZABLE"),
    ("reduce", _authority.__reduce__, RuntimeError, "FROZEN_ENTRY.AUTHORITY_NOT_SERIALIZABLE"),
    ("reduce-ex", lambda: _authority.__reduce_ex__(4), RuntimeError, "FROZEN_ENTRY.AUTHORITY_NOT_SERIALIZABLE"),
):
    _handoff_checks.append(_expect_rejection(_label, _operation, _exception, _message))

_fixture_before_worker = _authority.read_verified("fixture")
_worker_checks, _worker_threads = _check_worker_rejections(_authority)
_handoff_checks.extend(_worker_checks)
_handoff_selftest["threads"] = _worker_threads
if _authority.read_verified("fixture") != _fixture_before_worker:
    raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_OWNER_READ_CHANGED")
_handoff_checks.append(("owner-read-after-worker", "OK"))


class _SourceSpec:
    def __init__(self, name, origin, loader):
        self.name = name
        self.origin = origin
        self.loader = loader


# Native direct compilation has no pathname loader or CWD-derived origin.
__loader__ = None
__spec__ = _SourceSpec(__name__, __file__, __loader__)


class TrustedSourceLoader:
    """Compile only source bytes obtained from the opaque native authority."""

    def __init__(self, authority, entry_id, module_name, origin):
        self._authority = authority
        self._entry_id = entry_id
        self._module_name = module_name
        self._origin = origin
        self.attestation = None

    def execute(self):
        source = self._authority.read_verified(self._entry_id)
        proof = self._authority.module_reproof(self._entry_id)
        code = compile(source, self._origin, "exec", dont_inherit=True)
        namespace = {
            "__builtins__": __builtins__,
            "__file__": self._origin,
            "__loader__": self,
            "__name__": self._module_name,
        }
        namespace["__spec__"] = _SourceSpec(self._module_name, self._origin, self)
        exec(code, namespace, namespace)
        if code.co_filename != namespace["__file__"]:
            raise RuntimeError("FROZEN_ENTRY.SOURCE_METADATA_MISMATCH")
        if namespace["__spec__"].origin != namespace["__file__"]:
            raise RuntimeError("FROZEN_ENTRY.SOURCE_ORIGIN_MISMATCH")
        self.attestation = (self._entry_id, proof, code.co_filename, id(self))
        return namespace


_loader = TrustedSourceLoader(
    _authority,
    "critical-source",
    "localcat_spike_critical",
    "_internal/localcat_spike_critical.py",
)
_saved_read = _authority.read_verified
_saved_reproof = _authority.module_reproof
_saved_close = _authority.close
_saved_loader_execute = _loader.execute
_critical = _loader.execute()
_fixture = _authority.read_verified("fixture")
if not _critical["verify_fixture"](_fixture):
    raise RuntimeError("FROZEN_ENTRY.FIXTURE_CONTRACT_FAILED")
if not _authority.module_reproof("python-runtime")[0]:
    raise RuntimeError("FROZEN_ENTRY.PYTHON_REPROOF_FAILED")
if not _authority.module_reproof("vcruntime-runtime")[0]:
    raise RuntimeError("FROZEN_ENTRY.VCRUNTIME_REPROOF_FAILED")

if _authority.close() is not True:
    raise RuntimeError("FROZEN_ENTRY.HANDOFF_SELFTEST_CLOSE_FAILED")
_handoff_checks.append(("close", "OK"))
for _label, _operation in (
    ("saved-read-after-close", lambda: _saved_read("fixture")),
    ("saved-reproof-after-close", lambda: _saved_reproof("fixture")),
    ("saved-close-after-close", _saved_close),
    ("saved-loader-after-close", _saved_loader_execute),
    ("read-after-close", lambda: _authority.read_verified("fixture")),
    ("reproof-after-close", lambda: _authority.module_reproof("fixture")),
    ("close-after-close", _authority.close),
):
    _handoff_checks.append(_expect_rejection(_label, _operation, RuntimeError,
                                           "FROZEN_ENTRY.AUTHORITY_UNAVAILABLE"))
_handoff_checks.append(_expect_rejection("take-after-close", _native.take_attestation,
                                       RuntimeError, "FROZEN_ENTRY.HANDOFF_UNAVAILABLE"))
_handoff_selftest["checks"] = tuple(_handoff_checks)
_handoff_selftest["state"] = "COMPLETE"
