"""Capability-gated public execution port for pinned text-v1 matching."""

from __future__ import annotations

from typing import final

from matcher_capability import MatcherCapabilityPublisher
from text_matcher import TEXT_MATCHER_SEMANTICS_VERSION, TextMatcherV1
from tm_contracts import (
    TextMatchRejected,
    TextMatchRequest,
    TextMatchSuccess,
    TextMatcherCapability,
    TextMatchOutcome,
    _text_match_matrix_reject_code,
)


def _require_publisher(
    value: object,
) -> MatcherCapabilityPublisher:
    if type(value) is not MatcherCapabilityPublisher:
        raise TypeError(
            "publisher must be MatcherCapabilityPublisher"
        )
    return value


@final
class CapabilityGatedTextMatcherV1:
    """Execute text-v1 only under one evaluator-produced snapshot."""

    __slots__: tuple[str, ...] = ("__matcher", "__publisher")

    def __init__(
        self,
        publisher: MatcherCapabilityPublisher,
    ) -> None:
        self.__publisher = _require_publisher(publisher)
        if (
            self.__publisher.semantics_version
            != TEXT_MATCHER_SEMANTICS_VERSION
        ):
            raise ValueError(
                "publisher semantics version does not match TextMatcherV1"
            )
        self.__matcher = TextMatcherV1()

    def capability(self) -> TextMatcherCapability:
        """Return the current immutable capability for display only."""

        return self.__publisher.snapshot()

    def match(self, request: TextMatchRequest) -> TextMatchOutcome:
        """Authorize and execute against exactly one capability snapshot."""

        if not isinstance(request, TextMatchRequest):
            raise TypeError("request must be TextMatchRequest")

        capability = self.__publisher.snapshot()
        reject_code = _text_match_matrix_reject_code(
            capability=capability,
            profile=request.profile,
            options=request.options,
        )
        request_digest = request.request_digest
        if reject_code is not None:
            return TextMatchRejected(
                code=reject_code,
                safe_reason=f"MATCHER.{reject_code.value}",
                request_profile=request.profile,
                request_options=request.options,
                request_digest=request_digest,
                capability=capability,
            )

        hits = self.__matcher.match(
            text=request.text,
            query=request.query,
            profile=request.profile,
            options=request.options,
        )
        return TextMatchSuccess(
            hits=hits,
            request_profile=request.profile,
            request_options=request.options,
            request_digest=request_digest,
            capability=capability,
        )


__all__ = ["CapabilityGatedTextMatcherV1"]
