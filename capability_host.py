"""Qt-free host for the Matcher Gate and fail-closed TM retrieval state.

The host starts exact-only, then lets the application composition owner rerun
the Core validated matcher factory for the loaded checkout.  Ordinary callers
only receive immutable matcher snapshots and generation notifications.  The
later retrieval Gate C/D tasks replace only their independent retrieval
handoff; no capability is inferred from booleans, store health, or display
state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path
import stat
import sys
from threading import Condition, RLock
from types import CodeType, FunctionType, ModuleType
from typing import Any, Protocol, cast, final, runtime_checkable

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
    RetrievalCapabilityPublisher,
    default_retrieval_capability_publisher,
)


_MATCHER_CLOSED_REASON = "MATCHER.VALIDATION_UNAVAILABLE"
_COMPOSITION_MINT_IDENTITY = object()


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
) -> tuple[tuple[int, str, str], ...] | None:
    if values is None:
        return None
    observed: list[tuple[int, str, str]] = []
    for raw_cell in values:
        cell = raw_cell
        try:
            contents = cast(Any, cell).cell_contents
        except ValueError:
            observed.append((id(cell), "EMPTY", ""))
        else:
            observed.append(
                (id(cell), type(contents).__qualname__, repr(contents))
            )
    return tuple(observed)


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
    closure_snapshot: tuple[tuple[int, str, str], ...] | None
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
        if type(self.query_port) is not _ExactOnlyRetrievalQueryPort:
            raise TypeError("retrieval query port must be host-owned")
        if type(self.display) is not RetrievalDisplayState:
            raise TypeError("retrieval display must be RetrievalDisplayState")


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


@final
@dataclass(frozen=True, slots=True)
class _ExactOnlyRetrievalQueryPort:
    """Keep the mutable Core publisher/service graph host-private."""

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

        if owner_identity is not self.__owner_identity:
            raise PermissionError(
                "matcher generation publication requires composition owner"
            )
        _require_generation(generation)
        if generation <= self.__generation:
            raise ValueError("matcher generation must increase")
        self.__generation = generation
        self.__condition.notify_all()


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
    fts5_available, _ = capability.fuzzy_available_for("FTS5_TRIGRAM")
    fallback_available, _ = capability.fuzzy_available_for("GRAM_FALLBACK")
    if capability.context.available or fts5_available or fallback_available:
        raise RuntimeError("exact-only bootstrap received an open retrieval gate")
    return RetrievalDisplayState(
        context_available=False,
        fuzzy_available=False,
        safe_codes=capability.summary.unavailable_codes,
    )


@final
class CapabilityHost:
    """Hold immutable process handoffs for independent matcher/retrieval gates.

    Matcher validation mutation is isolated behind a composition-owner object;
    the ordinary host surface accepts no evidence, manifest, caller flag, or
    store health.  Retrieval Gate C/D replacement remains in its later tasks.
    """

    __slots__ = (
        "__lock",
        "__matcher_handoff",
        "__matcher_notifications",
        "__matcher_owner_identity",
        "__retrieval_publisher",
        "__retrieval_service",
        "__retrieval_handoff",
        "__status",
    )

    def __init__(self, *, evaluated_at_utc: datetime) -> None:
        publisher = default_retrieval_capability_publisher(evaluated_at_utc)
        retrieval_display = _exact_only_retrieval_display(publisher)
        service = TMRetrievalService(capability_publisher=publisher)
        retrieval = RetrievalHandoffSnapshot(
            generation=0,
            query_port=_ExactOnlyRetrievalQueryPort(service),
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
        self.__matcher_handoff = matcher
        self.__matcher_owner_identity = object()
        self.__matcher_notifications = _MatcherGenerationNotifications(
            self.__lock,
            self.__matcher_owner_identity,
        )
        self.__retrieval_publisher = publisher
        self.__retrieval_service = service
        self.__retrieval_handoff = retrieval
        self.__status = CapabilityDisplaySnapshot(
            matcher=matcher.display,
            retrieval=retrieval.display,
        )

    def matcher_snapshot(self) -> MatcherHandoffSnapshot:
        """Capture one immutable matcher handoff reference."""

        with self.__lock:
            return self.__matcher_handoff

    def matcher_generation_notifications(
        self,
    ) -> MatcherGenerationNotificationPort:
        """Return the read-only generation-change observation port."""

        return self.__matcher_notifications

    def retrieval_snapshot(self) -> RetrievalHandoffSnapshot:
        """Capture one immutable retrieval handoff reference."""

        with self.__lock:
            return self.__retrieval_handoff

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
            handoff = MatcherHandoffSnapshot(
                generation=generation,
                matcher=exposed_matcher,
                display=display,
            )
            status = CapabilityDisplaySnapshot(
                matcher=handoff.display,
                retrieval=self.__retrieval_handoff.display,
            )
            self.__matcher_handoff = handoff
            self.__status = status
            self.__matcher_notifications._publish_locked(
                self.__matcher_owner_identity,
                generation,
            )
            return handoff


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


@dataclass(frozen=True, slots=True)
class CapabilityHostComposition:
    """Split the runtime read port from its composition-owner validation port."""

    host: CapabilityHost
    matcher_validation_owner: MatcherValidationOwnerPort

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


def compose_capability_host(
    *,
    evaluated_at_utc: datetime,
) -> CapabilityHostComposition:
    """Create the application-owned host and its private validation control."""

    host = CapabilityHost(evaluated_at_utc=evaluated_at_utc)
    return CapabilityHostComposition(
        host=host,
        matcher_validation_owner=host._composition_matcher_owner(
            _COMPOSITION_MINT_IDENTITY,
        ),
    )


__all__ = [
    "CapabilityHostComposition",
    "CapabilityDisplaySnapshot",
    "CapabilityHost",
    "MatcherGenerationNotificationPort",
    "MatcherHandoffSnapshot",
    "MatcherValidationOwnerPort",
    "RetrievalHandoffSnapshot",
    "RetrievalQueryPort",
    "compose_capability_host",
]
