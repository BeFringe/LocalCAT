"""Reusable Wave 0 doubles for Parser terminal and view contract tests.

These helpers deliberately do not implement the production guarded session.
They only make hostile raw-event traces deterministic so later Foundation tests
can prove that provisional records are never mistaken for commit authority.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Final, Generic, Iterable, Iterator, Literal, NoReturn, TypeVar


Severity = Literal["warning", "fatal"]


@dataclass(frozen=True, slots=True)
class StubRecord:
    local_id: str
    value: str

    def __post_init__(self) -> None:
        if type(self.local_id) is not str or not self.local_id:
            raise ValueError("local_id must be a non-empty exact string")
        if type(self.value) is not str:
            raise TypeError("value must be an exact string")


@dataclass(frozen=True, slots=True)
class StubIssue:
    code: str
    severity: Severity
    record_ordinal: int | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or not self.code:
            raise ValueError("code must be a non-empty exact string")
        if self.severity not in ("warning", "fatal"):
            raise ValueError("severity must be warning or fatal")
        if self.record_ordinal is not None and (
            type(self.record_ordinal) is not int or self.record_ordinal < 1
        ):
            raise ValueError("record_ordinal must be a positive exact int or None")


RawEvent = StubRecord | StubIssue


class StreamEnd(str, Enum):
    OPEN = "open"
    NATURAL_EOF = "natural_eof"
    EARLY_CLOSE = "early_close"
    RAW_EXCEPTION = "raw_exception"


class InjectedRawFailure(RuntimeError):
    """Finite stand-in for a raw iterator that never proves natural EOF."""


class InjectedConsumerFailure(RuntimeError):
    """Failure raised by a consumer after a configured provisional event."""


EventT = TypeVar("EventT")


class ScriptedRawIterator(Generic[EventT], Iterator[EventT]):
    """Injectable raw-event iterator with observable termination state."""

    def __init__(
        self,
        events: Iterable[EventT],
        *,
        fail_instead_of_eof: bool = False,
    ) -> None:
        if type(fail_instead_of_eof) is not bool:
            raise TypeError("fail_instead_of_eof must be an exact bool")
        self._events = tuple(events)
        self._index = 0
        self._fail_instead_of_eof = fail_instead_of_eof
        self.end = StreamEnd.OPEN

    def __iter__(self) -> ScriptedRawIterator[EventT]:
        return self

    def __next__(self) -> EventT:
        if self.end is StreamEnd.RAW_EXCEPTION:
            raise InjectedRawFailure("raw stream ended without an observable EOF proof")
        if self.end is not StreamEnd.OPEN:
            raise StopIteration
        if self._index < len(self._events):
            event = self._events[self._index]
            self._index += 1
            return event
        if self._fail_instead_of_eof:
            self.end = StreamEnd.RAW_EXCEPTION
            raise InjectedRawFailure("raw stream ended without an observable EOF proof")
        self.end = StreamEnd.NATURAL_EOF
        raise StopIteration

    def close(self) -> None:
        if self.end is StreamEnd.OPEN:
            self.end = StreamEnd.EARLY_CLOSE


@dataclass(frozen=True, slots=True)
class EventView:
    records: tuple[StubRecord, ...]
    issues: tuple[StubIssue, ...]
    record_count: int
    warning_count: int
    fatal_count: int
    issue_counts: tuple[tuple[str, int], ...]
    event_order: tuple[tuple[str, str], ...]


def project_event_view(events: Iterable[RawEvent]) -> EventView:
    materialized = tuple(events)
    unknown = tuple(
        event
        for event in materialized
        if not isinstance(event, (StubRecord, StubIssue))
    )
    if unknown:
        raise TypeError(f"unsupported stub event type: {type(unknown[0]).__name__}")
    records = tuple(event for event in materialized if isinstance(event, StubRecord))
    issues = tuple(event for event in materialized if isinstance(event, StubIssue))
    counts = Counter(issue.code for issue in issues)
    order = tuple(
        ("record", event.local_id)
        if isinstance(event, StubRecord)
        else (event.severity, event.code)
        for event in materialized
    )
    return EventView(
        records=records,
        issues=issues,
        record_count=len(records),
        warning_count=sum(issue.severity == "warning" for issue in issues),
        fatal_count=sum(issue.severity == "fatal" for issue in issues),
        issue_counts=tuple(sorted(counts.items())),
        event_order=order,
    )


def assert_views_equivalent(left: EventView, right: EventView) -> None:
    """Compare all fields required by iterator/materialized equivalence."""

    if left != right:
        raise AssertionError(f"event views differ:\niterator={left!r}\nmaterialized={right!r}")


def record_then_fatal_tail() -> tuple[RawEvent, ...]:
    return (
        StubRecord(local_id="record-1", value="provisional"),
        StubIssue(
            code="PARSER.SYNTAX.MALFORMED",
            severity="fatal",
            record_ordinal=2,
        ),
    )


def consume_with_injected_failure(
    stream: ScriptedRawIterator[RawEvent],
    *,
    fail_after_events: int,
) -> NoReturn:
    if type(fail_after_events) is not int or fail_after_events < 1:
        raise ValueError("fail_after_events must be a positive exact int")
    observed = 0
    for _event in stream:
        observed += 1
        if observed == fail_after_events:
            raise InjectedConsumerFailure(
                f"consumer failed after {fail_after_events} provisional event(s)"
            )
    raise AssertionError("stream ended before the configured consumer failure")


VALID_EVENTS: Final[tuple[RawEvent, ...]] = (
    StubRecord(local_id="record-1", value="first"),
    StubIssue(code="PARSER.TEST.WARNING", severity="warning", record_ordinal=2),
    StubRecord(local_id="record-2", value="second"),
)
