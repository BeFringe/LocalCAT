"""Qt-free compatibility adapter for legacy and configured term records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import final

from capability_host import MatcherHandoffSnapshot
from editor_contracts import TermMatchPolicy, TermRecord
from glossary_engine import GlossaryEngine, TermHit
from tm_contracts import (
    SearchOptions,
    TextMatchProfile,
    TextMatchRejected,
    TextMatchRequest,
    TextMatchSuccess,
    TextMatcherState,
)


@final
class ConfiguredTermAdapterError(RuntimeError):
    """Content-free failure to execute the configured matcher cohort."""

    code: str

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _RankedTermHit:
    hit: TermHit
    record_order: int
    hit_order: int


@dataclass(frozen=True, slots=True)
class _ResourceRankedTermHit:
    hit: TermHit
    resource_order: int
    record_order: int


@final
class ConfiguredTermAdapter:
    """Expose one mixed termbase through the existing ``GlossaryEngine`` seam.

    Legacy rows always use the case-sensitive substring Trie.  Until the
    handoff authorizes ``CONFIGURABLE_TEXT_V1``, v1 rows join that same Trie
    cohort and their persisted flags deliberately have no effect.  Once
    authorized, only v1 rows are delegated to the exact Core matcher carried
    by the supplied handoff snapshot.
    """

    __slots__ = (
        "_configured_records",
        "_glossary_source",
        "_handoff",
        "_legacy_engine",
        "_legacy_record_order",
    )

    def __init__(
        self,
        records: tuple[TermRecord, ...],
        glossary_source: str,
        handoff: MatcherHandoffSnapshot,
    ) -> None:
        if type(records) is not tuple:
            raise TypeError("term records must be an exact tuple")
        if any(type(record) is not TermRecord for record in records):
            raise TypeError("term records must contain exact TermRecord values")
        for record in records:
            record.__post_init__()
        if type(glossary_source) is not str:
            raise TypeError("glossary source must be an exact string")
        if not glossary_source.strip():
            raise ValueError("glossary source must not be empty")
        if type(handoff) is not MatcherHandoffSnapshot:
            raise TypeError("term matcher handoff must be MatcherHandoffSnapshot")
        handoff.__post_init__()

        row_ordinals = tuple(record.locator.row_ordinal for record in records)
        if row_ordinals != tuple(sorted(row_ordinals)):
            raise ValueError("term records must retain persisted record order")
        sources = tuple(record.source for record in records)
        if len(sources) != len(set(sources)):
            raise ValueError("term records must not contain duplicate sources")

        configured_enabled = (
            handoff.display.state is TextMatcherState.TEXT_V1_VALIDATED
            and TextMatchProfile.CONFIGURABLE_TEXT_V1
            in handoff.display.supported_profiles
        )
        legacy_engine = GlossaryEngine()
        legacy_record_order: dict[str, int] = {}
        configured_records: list[tuple[int, TermRecord]] = []
        for record_order, record in enumerate(records):
            if (
                configured_enabled
                and record.policy is TermMatchPolicy.CONFIGURED
            ):
                configured_records.append((record_order, record))
                continue
            legacy_engine.add_term(
                record.source,
                record.target,
                glossary_source,
            )
            legacy_record_order[record.source] = record_order

        self._glossary_source = glossary_source
        self._handoff = handoff
        self._legacy_engine = legacy_engine
        self._legacy_record_order = legacy_record_order
        self._configured_records = tuple(configured_records)

    def extract_terms(self, text: str) -> list[TermHit]:
        """Return all overlaps in start/length/record order.

        The result stays in the legacy ``TermHit`` shape, so the existing
        longest-first, non-overlapping presentation selection remains the
        sole selection policy.
        """

        if type(text) is not str:
            raise TypeError("term match text must be an exact string")

        ranked: list[_RankedTermHit] = []
        for hit_order, hit in enumerate(self._legacy_engine.extract_terms(text)):
            record_order = self._legacy_record_order.get(hit.source_term)
            if record_order is None:
                raise AssertionError("legacy Trie returned an unknown term")
            ranked.append(
                _RankedTermHit(
                    hit=hit,
                    record_order=record_order,
                    hit_order=hit_order,
                )
            )

        if self._configured_records:
            matcher = self._handoff.matcher
            if matcher is None:
                raise ConfiguredTermAdapterError("MATCHER.PORT_UNAVAILABLE")
            capability = matcher.capability()
            if (
                capability.state is not self._handoff.display.state
                or capability.supported_profiles
                != self._handoff.display.supported_profiles
            ):
                raise ConfiguredTermAdapterError("MATCHER.HANDOFF_STALE")

            configured_hit_order = len(ranked)
            for record_order, record in self._configured_records:
                match_case = record.match_case
                whole_word = record.whole_word
                if type(match_case) is not bool or type(whole_word) is not bool:
                    raise AssertionError("configured term flags became invalid")
                request = TextMatchRequest(
                    text=text,
                    query=record.source,
                    profile=TextMatchProfile.CONFIGURABLE_TEXT_V1,
                    options=SearchOptions(
                        match_case=match_case,
                        whole_word=whole_word,
                    ),
                )
                outcome = matcher.match(request)
                if type(outcome) is TextMatchRejected:
                    raise ConfiguredTermAdapterError(outcome.safe_reason)
                if type(outcome) is not TextMatchSuccess:
                    raise TypeError("Core matcher returned an unsupported outcome")
                if (
                    outcome.request_digest != request.request_digest
                    or outcome.request_profile
                    is not TextMatchProfile.CONFIGURABLE_TEXT_V1
                    or outcome.request_options != request.options
                ):
                    raise ConfiguredTermAdapterError(
                        "MATCHER.OUTCOME_MISMATCH"
                    )
                if outcome.capability != capability:
                    raise ConfiguredTermAdapterError(
                        "MATCHER.CAPABILITY_CHANGED"
                    )
                for core_hit in outcome.hits:
                    if core_hit.end_index > len(text):
                        raise ConfiguredTermAdapterError(
                            "MATCHER.OFFSET_OUT_OF_RANGE"
                        )
                    ranked.append(
                        _RankedTermHit(
                            hit=TermHit(
                                source_term=text[
                                    core_hit.start_index : core_hit.end_index
                                ],
                                target_term=record.target,
                                start_index=core_hit.start_index,
                                end_index=core_hit.end_index,
                                glossary_source=self._glossary_source,
                            ),
                            record_order=record_order,
                            hit_order=configured_hit_order,
                        )
                    )
                    configured_hit_order += 1

        ranked.sort(
            key=lambda candidate: (
                candidate.hit.start_index,
                -(candidate.hit.end_index - candidate.hit.start_index),
                candidate.record_order,
                candidate.hit_order,
            )
        )
        return [candidate.hit for candidate in ranked]


def extract_terms_from_resources(
    text: str,
    adapters: tuple[ConfiguredTermAdapter, ...],
) -> list[TermHit]:
    """Extract and globally merge resources in declarative input order.

    The function owns the complete domain sort.  Callers only supply adapters
    in resource order and must not reproduce start/length/record ranking.
    """

    if type(text) is not str:
        raise TypeError("term match text must be an exact string")
    if type(adapters) is not tuple:
        raise TypeError("term adapters must be an exact tuple")
    if any(type(adapter) is not ConfiguredTermAdapter for adapter in adapters):
        raise TypeError(
            "term adapters must contain exact ConfiguredTermAdapter values"
        )

    ranked: list[_ResourceRankedTermHit] = []
    for resource_order, adapter in enumerate(adapters):
        ranked.extend(
            _ResourceRankedTermHit(
                hit=hit,
                resource_order=resource_order,
                record_order=record_order,
            )
            for record_order, hit in enumerate(adapter.extract_terms(text))
        )
    ranked.sort(
        key=lambda resource_hit: (
            resource_hit.hit.start_index,
            -(
                resource_hit.hit.end_index
                - resource_hit.hit.start_index
            ),
            resource_hit.resource_order,
            resource_hit.record_order,
        )
    )
    return [resource_hit.hit for resource_hit in ranked]


__all__ = [
    "ConfiguredTermAdapter",
    "ConfiguredTermAdapterError",
    "extract_terms_from_resources",
]
