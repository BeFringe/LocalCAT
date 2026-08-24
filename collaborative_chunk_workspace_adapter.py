"""Trusted composition from the live Workspace owner into Chunk leaf facts.

This adapter is deliberately the only production seam that imports both
authorities.  It first asks the exact live Workspace owner to revalidate its
issued projection, then translates only body-free identity/presence facts.
"""

from __future__ import annotations

from collaborative_chunk_contracts import (
    ChunkError,
    ChunkPublishedUniverseBinding,
    ChunkPublishedUniverseProjection,
    ChunkPublishedWorkspaceTransition,
    ChunkProgressSegmentFact,
    ChunkSegmentRef,
    ChunkUniverseEntry,
    ChunkWorkspaceBinding,
    ChunkWorkspaceProgressProjection,
    ChunkWorkspaceUniverseProjection,
    chunk_published_workspace_transition_digest_v1,
    validate_chunk_published_workspace_transition,
)
from project_workspace import (
    ProjectWorkspaceService,
    PublishedWorkspaceTransitionProjection,
    WorkspaceUniverseProjection,
)
from project_workspace_contracts import SegmentIdentity
from project_workspace_identity import ProjectWorkspaceError


def _fail(code: str) -> None:
    raise ChunkError(code)


def capture_live_workspace_transition(
    workspace_owner: ProjectWorkspaceService,
    projection: PublishedWorkspaceTransitionProjection,
) -> ChunkPublishedWorkspaceTransition:
    """Translate only after exact live-owner issuance revalidation succeeds."""

    if type(workspace_owner) is not ProjectWorkspaceService:
        _fail("CHUNK.CONTRACT_INVALID")
    if type(projection) is not PublishedWorkspaceTransitionProjection:
        _fail("CHUNK.CONTRACT_INVALID")
    try:
        validated = workspace_owner.validate_published_workspace_transition(
            projection
        )
    except ProjectWorkspaceError:
        raise ChunkError("CHUNK.PREVIEW_STALE") from None
    if validated is not projection:
        _fail("CHUNK.PREVIEW_STALE")

    project_id = projection.current.binding.project_id

    def translate_universe(source: object) -> ChunkPublishedUniverseProjection:
        binding = source.binding
        return ChunkPublishedUniverseProjection(
            binding=ChunkPublishedUniverseBinding(
                project_id=binding.project_id,
                workspace_session_id=binding.workspace_session_id,
                workspace_revision=binding.workspace_revision,
                workspace_composition_revision=(
                    binding.workspace_composition_revision
                ),
                workspace_digest=binding.workspace_digest,
                segment_universe_digest=binding.segment_universe_digest,
            ),
            entries=tuple(
                ChunkUniverseEntry(
                    segment=ChunkSegmentRef(
                        project_id=project_id,
                        identity=SegmentIdentity(
                            entry.identity.document_id,
                            entry.identity.local_segment_id,
                        ),
                    ),
                    source_presence=entry.source_presence,
                )
                for entry in source.entries
            ),
        )

    previous = translate_universe(projection.previous)
    current = translate_universe(projection.current)
    source_changed = tuple(
        ChunkSegmentRef(
            project_id=project_id,
            identity=SegmentIdentity(
                identity.document_id,
                identity.local_segment_id,
            ),
        )
        for identity in projection.source_changed_identities
    )
    transition_digest = chunk_published_workspace_transition_digest_v1(
        projection.operation_id,
        previous,
        current,
        source_changed,
    )
    return validate_chunk_published_workspace_transition(
        ChunkPublishedWorkspaceTransition(
            operation_id=projection.operation_id,
            previous=previous,
            current=current,
            source_changed_members=source_changed,
            transition_digest=transition_digest,
        )
    )


def capture_live_workspace_universe(
    workspace_owner: ProjectWorkspaceService,
) -> ChunkWorkspaceUniverseProjection:
    """Translate an owner-issued live universe into Chunk leaf facts."""

    if type(workspace_owner) is not ProjectWorkspaceService:
        _fail("CHUNK.CONTRACT_INVALID")
    try:
        issued = workspace_owner.capture_workspace_universe()
        validated = workspace_owner.validate_workspace_universe(issued)
    except ProjectWorkspaceError:
        raise ChunkError("CHUNK.PERMISSION_STALE") from None
    if validated is not issued or type(issued) is not WorkspaceUniverseProjection:
        _fail("CHUNK.PERMISSION_STALE")
    binding = issued.binding
    projection = ChunkWorkspaceUniverseProjection(
        binding=ChunkWorkspaceBinding(
            project_id=binding.project_id,
            workspace_session_id=binding.workspace_session_id,
            workspace_revision=binding.workspace_revision,
            segment_universe_digest=binding.segment_universe_digest,
            workspace_composition_revision=(
                binding.workspace_composition_revision
            ),
        ),
        entries=tuple(
            ChunkUniverseEntry(
                segment=ChunkSegmentRef(
                    project_id=binding.project_id,
                    identity=SegmentIdentity(
                        entry.identity.document_id,
                        entry.identity.local_segment_id,
                    ),
                ),
                source_presence=entry.source_presence,
            )
            for entry in issued.entries
        ),
    )
    projection.__post_init__()
    return projection


def capture_live_workspace_progress(
    workspace_owner: ProjectWorkspaceService,
) -> ChunkWorkspaceProgressProjection:
    """Derive body-free progress only after validating the live universe.

    Target text is observed solely to classify the Workspace-owned strip-empty
    rule.  No source, target, speaker, path, display name, or body digest is
    copied into the downstream projection.
    """

    if type(workspace_owner) is not ProjectWorkspaceService:
        _fail("CHUNK.CONTRACT_INVALID")
    before = capture_live_workspace_universe(workspace_owner)
    flat = workspace_owner.flat_segments
    by_identity = {entry.segment.identity: entry for entry in before.entries}
    if len(by_identity) != len(before.entries) or len(flat) != len(before.entries):
        _fail("CHUNK.PERMISSION_STALE")
    facts = []
    for item in flat:
        identity = SegmentIdentity(
            item.identity.document_id,
            item.identity.local_segment_id,
        )
        universe_entry = by_identity.get(identity)
        if universe_entry is None:
            _fail("CHUNK.PERMISSION_STALE")
        facts.append(
            ChunkProgressSegmentFact(
                segment=universe_entry.segment,
                source_presence=universe_entry.source_presence,
                target_is_blank=not bool(item.segment.target.strip()),
                confirmed=item.segment.confirmed,
            )
        )
    after = capture_live_workspace_universe(workspace_owner)
    if after != before:
        _fail("CHUNK.PERMISSION_STALE")
    projection = ChunkWorkspaceProgressProjection(
        binding=before.binding,
        entries=tuple(facts),
    )
    projection.__post_init__()
    return projection


__all__ = [
    "capture_live_workspace_transition",
    "capture_live_workspace_progress",
    "capture_live_workspace_universe",
]
