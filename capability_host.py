"""Qt-free bootstrap host for fail-closed matcher and TM retrieval state.

This module intentionally owns only the exact-only process bootstrap.  The
formal Matcher Gate and retrieval Gate C/D compositions replace these
handoffs in their owning tasks; callers cannot promote capability through
booleans, store health, or display state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Protocol, final

from editor_contracts import RetrievalDisplayState, TextMatcherDisplayState
from tm_contracts import (
    CapabilityGatedTextMatcher,
    QueryReport,
    TMQuery,
    TMResourceHandle,
    TextMatcherState,
)
from tm_retrieval import TMRetrievalService
from tm_retrieval_capability import (
    RetrievalCapabilityPublisher,
    default_retrieval_capability_publisher,
)


_MATCHER_CLOSED_REASON = "MATCHER.VALIDATION_UNAVAILABLE"


def _require_generation(value: object) -> None:
    if type(value) is not int:
        raise TypeError("capability generation must be an exact integer")
    if value < 0:
        raise ValueError("capability generation must be non-negative")


@dataclass(frozen=True, slots=True)
class MatcherHandoffSnapshot:
    """One immutable matcher handoff captured by a search operation."""

    generation: int
    matcher: CapabilityGatedTextMatcher | None
    display: TextMatcherDisplayState

    def __post_init__(self) -> None:
        _require_generation(self.generation)
        if self.matcher is not None:
            if not callable(getattr(self.matcher, "capability", None)):
                raise TypeError("matcher must expose the Core capability port")
            if not callable(getattr(self.matcher, "match", None)):
                raise TypeError("matcher must expose the Core match port")
        if type(self.display) is not TextMatcherDisplayState:
            raise TypeError("matcher display must be TextMatcherDisplayState")
        if self.matcher is None and self.display.state is not TextMatcherState.UNAVAILABLE:
            raise ValueError("missing matcher requires an unavailable display")
        if self.matcher is not None and self.display.state is TextMatcherState.UNAVAILABLE:
            raise ValueError("unavailable matcher display cannot expose a matcher")


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
    """Hold the immutable exact-only process bootstrap handoffs.

    No evidence, manifest, caller flag, or store health is accepted here.
    Formal capability refresh and atomic handoff replacement belong to the
    subsequent Matcher Gate and Gate C/D tasks.
    """

    __slots__ = (
        "__lock",
        "__matcher_handoff",
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

        self.__lock = Lock()
        self.__matcher_handoff = matcher
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

    def retrieval_snapshot(self) -> RetrievalHandoffSnapshot:
        """Capture one immutable retrieval handoff reference."""

        with self.__lock:
            return self.__retrieval_handoff

    def status_snapshot(self) -> CapabilityDisplaySnapshot:
        """Capture the matching safe display projection."""

        with self.__lock:
            return self.__status


__all__ = [
    "CapabilityDisplaySnapshot",
    "CapabilityHost",
    "MatcherHandoffSnapshot",
    "RetrievalHandoffSnapshot",
    "RetrievalQueryPort",
]
