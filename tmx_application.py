"""Application composition for exact-scope TMX export.

This module connects the Resource, Workspace, and Chunk owner seams to the
TMX profile and direct artifact publisher.  It owns no XML grammar, owner
identity, or ResourcePackage transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from chunk_controller_adapter import ChunkControllerAdapter
from editor_contracts import ResourceKind
from editor_controller import EditorController
from resource_repository import ResourceError, ResourceRepository
from tm_engine import TMEngine
from tmx_artifact_save import TmxDirectArtifactSaver
from tmx_context_contracts import (
    TmxDirectPlan,
    TmxDirectReceipt,
    TmxEffectiveLocales,
    TmxExportPreview,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_context_interchange import ParserTmxColdValidator, prepare_tmx_payload
from tmx_export_coordinator import ChunkTmxScopeAdapter, TmxExportCoordinator
from tmx_export_scope_contracts import (
    EntireProjectScopeMaterialization,
    ManagedResourceScopeMaterialization,
    SelectedChunkScopeMaterialization,
    TmxScopeCoordinatorError,
)


class TmxApplicationError(RuntimeError):
    """One body-safe composition failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise ValueError("TMX application error code must be non-empty")
        self.code = code
        super().__init__(code)


class _WorkspaceOwner:
    """Adapt the Controller's paired Workspace issue/revalidate seam."""

    __slots__ = ("_controller",)

    def __init__(self, controller: EditorController) -> None:
        self._controller = controller

    def capture_session_view(self):
        session, _universe = self._controller.issue_tmx_workspace_scope()
        return session

    def capture_workspace_universe(self):
        _session, universe = self._controller.issue_tmx_workspace_scope()
        return universe

    def revalidate_workspace_universe(self, projection):
        _session, universe = self._controller.revalidate_tmx_workspace_scope(
            self._controller.workspace_view,
            projection,
        )
        return universe


class _Materialization(Protocol):
    tmx_binding: TmxScopeBinding
    units: tuple


@dataclass(slots=True)
class TmxApplicationPreparation:
    """Opaque, single-use application preparation consumed by the Qt seam."""

    preview: TmxExportPreview
    _saver: TmxDirectArtifactSaver
    _plan: TmxDirectPlan
    _published: bool = False


class TmxExportApplicationService:
    """Prepare and publish Resource/Project/Chunk direct TMX artifacts."""

    __slots__ = ("_controller", "_chunk", "_repository", "_workspace")

    PROJECT_SCOPE_TOKEN = "project"
    CHUNK_SCOPE_PREFIX = "chunk:"

    def __init__(
        self,
        controller: EditorController,
        repository: ResourceRepository,
        *,
        chunk_controller: ChunkControllerAdapter | None = None,
    ) -> None:
        if type(controller) is not EditorController:
            raise TypeError("TMX application requires exact EditorController")
        if type(repository) is not ResourceRepository:
            raise TypeError("TMX application requires exact ResourceRepository")
        if chunk_controller is not None and type(chunk_controller) is not ChunkControllerAdapter:
            raise TypeError("TMX chunk controller must be exact")
        self._controller = controller
        self._chunk = chunk_controller
        self._repository = repository
        self._workspace = _WorkspaceOwner(controller)

    @staticmethod
    def effective_locales(source_locale: str, target_locale: str) -> TmxEffectiveLocales:
        try:
            return TmxEffectiveLocales(source_locale, target_locale)
        except (TypeError, ValueError) as error:
            raise TmxApplicationError("TMX.LOCALE.INVALID") from error

    def available_project_scopes(self) -> tuple[tuple[str, str], ...]:
        """Return the project plus every currently active explicit chunk."""

        if not self._controller.has_workspace:
            return ()
        result: list[tuple[str, str]] = [(self.PROJECT_SCOPE_TOKEN, "整个项目")]
        if self._chunk is None:
            return tuple(result)
        try:
            view = self._chunk.project_view()
        except Exception:
            return tuple(result)
        for chunk in view.chunks:
            result.append((f"{self.CHUNK_SCOPE_PREFIX}{chunk.chunk_id}", chunk.name))
        return tuple(result)

    def prepare_project_export(
        self,
        scope_token: str,
        source_locale: str,
        target_locale: str,
        destination: Path,
    ) -> TmxApplicationPreparation:
        locales = self.effective_locales(source_locale, target_locale)
        coordinator = TmxExportCoordinator(
            workspace_owner=self._workspace,
            chunk_owner=(
                None
                if self._chunk is None
                else ChunkTmxScopeAdapter(
                    self._chunk.issue_scope_projection,
                    self._chunk.revalidate_scope_projection,
                )
            ),
        )
        if scope_token == self.PROJECT_SCOPE_TOKEN:
            materialized = coordinator.capture_entire_project()

            def revalidate() -> None:
                coordinator.revalidate_entire_project(materialized)

        elif type(scope_token) is str and scope_token.startswith(self.CHUNK_SCOPE_PREFIX):
            chunk_id = scope_token[len(self.CHUNK_SCOPE_PREFIX) :]
            if not chunk_id or self._chunk is None:
                raise TmxApplicationError("TMX.SCOPE.UNAVAILABLE")
            materialized = coordinator.capture_selected_chunk(chunk_id)

            def revalidate() -> None:
                coordinator.revalidate_selected_chunk(materialized)

        else:
            raise TmxApplicationError("TMX.SCOPE.INVALID")
        return self._prepare_direct(materialized, locales, destination, revalidate)

    def prepare_resource_export(
        self,
        resource_id: str,
        source_locale: str,
        target_locale: str,
        destination: Path,
    ) -> TmxApplicationPreparation:
        locales = self.effective_locales(source_locale, target_locale)
        resource, store = self._managed_tm_owner(resource_id)
        coordinator = TmxExportCoordinator(resource_owner=store)
        materialized = coordinator.capture_managed_resource()
        if materialized.binding.resource_id != resource.id:
            raise TmxApplicationError("TMX.SCOPE.FOREIGN")

        def revalidate() -> None:
            coordinator.revalidate_managed_resource(materialized)

        return self._prepare_direct(materialized, locales, destination, revalidate)

    def publish(self, preparation: TmxApplicationPreparation) -> TmxDirectReceipt:
        if type(preparation) is not TmxApplicationPreparation:
            raise TypeError("TMX publication requires exact preparation")
        if preparation._published:
            raise TmxApplicationError("TMX.PLAN.CONSUMED")
        preparation._published = True
        return preparation._saver.apply(preparation._plan)

    def _prepare_direct(
        self,
        materialized: _Materialization,
        locales: TmxEffectiveLocales,
        destination: Path,
        revalidate,
    ) -> TmxApplicationPreparation:
        if type(materialized) not in (
            EntireProjectScopeMaterialization,
            SelectedChunkScopeMaterialization,
            ManagedResourceScopeMaterialization,
        ):
            raise TypeError("TMX materialization must be exact")
        payload = prepare_tmx_payload(
            materialized.tmx_binding,
            locales,
            materialized.units,
        )

        def revalidate_exact(binding: TmxScopeBinding) -> None:
            if binding != materialized.tmx_binding:
                raise TmxApplicationError("TMX.SCOPE.STALE")
            revalidate()

        saver = TmxDirectArtifactSaver(
            ParserTmxColdValidator(),
            revalidate_exact,
        )
        preview, plan = saver.preview(
            materialized.tmx_binding,
            payload,
            destination.expanduser().absolute(),
        )
        return TmxApplicationPreparation(preview, saver, plan)

    def _managed_tm_owner(self, resource_id: str):
        try:
            resource = self._repository.get(resource_id)
        except ResourceError as error:
            raise TmxApplicationError("TMX.SCOPE.MISSING") from error
        if resource.kind is not ResourceKind.TRANSLATION_MEMORY:
            raise TmxApplicationError("TMX.SCOPE.KIND_MISMATCH")
        try:
            store = TMEngine(str(resource.path)).canonical_store
        except Exception as error:
            raise TmxApplicationError("TMX.SCOPE.UNAVAILABLE") from error
        if store is None:
            raise TmxApplicationError("TMX.SCOPE.UNAVAILABLE")
        if store.coordinator.resource_id != resource.id:
            raise TmxApplicationError("TMX.SCOPE.FOREIGN")
        return resource, store


__all__ = [
    "TmxApplicationError",
    "TmxApplicationPreparation",
    "TmxExportApplicationService",
]
