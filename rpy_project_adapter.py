"""Application coordination for selected TLs and a Project-owned package session.

The adapter never decodes codec-private bytes or owns editing state. A session
holds Project services and the temporary intake lease until first publication.
Callers serialize save/close with editing; asynchronous UI ownership belongs to
the Controller, not to this synchronous adapter.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from dataclasses import replace

from parser_contracts import (
    CodecDescriptor, CodecIdentity, EffectivePurpose, FormatId,
    SelectionHints, SelectionRequest,
)
from project_codec_settings import ProjectCodecAvailability, ProjectCodecRuntime
from project_package import (
    OpenedProjectPackage, ProjectPackageExportResult,
    ProjectPackagePersistenceBinding, ProjectPackageService,
)
from project_save import ProjectSaveService
from project_workspace import ProjectWorkspaceService
from project_workspace_contracts import ProjectOriginKind, ProjectWorkspaceError
from project_workspace_intake import (
    PreparedSelectedProjectDocuments, SelectedProjectDocumentsRequest,
    SelectedProjectDocumentsError, prepare_selected_project_documents,
)


_RPY_IDENTITY = CodecIdentity('localcat.rpy', 'renpy-tl', '1')
_RPY_FORMAT = FormatId('renpy-tl-v1')
_MAX_SELECTED_TL = 256


class RpyProjectSelectionError(ValueError):
    """Body-safe Application policy failure at an explicitly chosen file."""

    def __init__(self, code: str, source_ref: str, diagnostics=()) -> None:
        self.code, self.source_ref = code, source_ref
        self.diagnostics = diagnostics
        super().__init__(code)


class RpyProjectUnavailableError(RuntimeError):
    """A body-safe unavailable-provider reason for a new TL request."""

    def __init__(self, availability: ProjectCodecAvailability) -> None:
        self.availability = availability
        super().__init__(availability.code)


def _availability(runtime, identity, format_id) -> ProjectCodecAvailability:
    if type(runtime) is not ProjectCodecRuntime:
        raise TypeError('runtime must be exact ProjectCodecRuntime')
    for item in runtime.availability:
        if item.codec_identity == identity and item.format_id == format_id:
            return item
    return ProjectCodecAvailability(
        identity, format_id, False, 'PARSER.SELECTION.PROVIDER_INCOMPATIBLE',
        'This document requires a different Ren\'Py TL codec or profile.',
    )


def _require_single_tl(workspace) -> None:
    if (len(workspace.documents) != 1
            or workspace.origin.kind is not ProjectOriginKind.SINGLE_FILE
            or workspace.origin.profile_version != 'explicit-single-file-v1'):
        raise ProjectWorkspaceError('PROJECT.PACKAGE.FORMAT_UNSUPPORTED')
    document = workspace.documents[0]
    identity = document.codec_identity
    if (identity.provider_id != _RPY_IDENTITY.provider_id
            or identity.codec_id != _RPY_IDENTITY.codec_id
            or document.codec_private_member is None):
        raise ProjectWorkspaceError('PROJECT.PACKAGE.FORMAT_UNSUPPORTED')


class RpyProjectSession:
    """Closeable Application handle around the sole Project editing authority.

    The service objects remain the same after saving. A persistence binding is
    owner-issued data, not a success flag: only the returned durable receipt
    confirms publication. A recovery report is returned unchanged and blocks
    subsequent saves here until the caller uses the existing Project recovery
    workflow and opens a fresh session.
    """

    def __init__(
        self, package_service: ProjectPackageService,
        save_service: ProjectSaveService, *,
        prepared: PreparedSelectedProjectDocuments | None = None,
        persistence_binding: ProjectPackagePersistenceBinding | None = None,
        creation_pending: bool = False,
    ) -> None:
        self._package_service = package_service
        self._save_service = save_service
        self._prepared = prepared
        self._persistence_binding = persistence_binding
        self._recovery_required = False
        self._closed = False
        self._creation_pending = creation_pending

    def configure_creation(self, request: SelectedProjectDocumentsRequest) -> None:
        """Configure a never-installed import candidate exactly once."""
        self._require_live()
        service = self.workspace_service
        if (not self._creation_pending or self._persistence_binding is not None
                or self._prepared is None or service.revision != 0):
            raise ProjectWorkspaceError('PROJECT.WORKSPACE.SESSION_STALE')
        self._creation_pending = False
        workspace = replace(service.workspace, name=request.name,
                            source_locale=request.source_locale,
                            target_locale=request.target_locale)
        self._save_service = ProjectSaveService(ProjectWorkspaceService(
            workspace, service.origin_binding, session_id=service.session_id,
            revision=service.revision), baseline=None)

    @property
    def workspace_service(self) -> ProjectWorkspaceService:
        return self._save_service.workspace_service

    @property
    def save_service(self) -> ProjectSaveService:
        return self._save_service

    @property
    def persistence_binding(self) -> ProjectPackagePersistenceBinding | None:
        return self._persistence_binding

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def source_retained(self) -> bool:
        return self._prepared is not None and not self._prepared.closed

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    def _require_live(self) -> None:
        if self._closed:
            raise ProjectWorkspaceError('PROJECT.WORKSPACE.SESSION_STALE')
        if self._recovery_required:
            raise ProjectWorkspaceError('PROJECT.SAVE.RECOVERY_REQUIRED')

    def export_availability(self, runtime: ProjectCodecRuntime) -> ProjectCodecAvailability:
        """Describe exact live-codec availability, without preparing a writer.

        The caller supplies current configuration, including after settings
        change. A true value does not bypass later source/private validation.
        """
        self._require_live()
        documents = self.workspace_service.workspace.documents
        if len(documents) != 1:
            return ProjectCodecAvailability(
                _RPY_IDENTITY, _RPY_FORMAT, False, 'RPY.EXPORT.UNAVAILABLE',
                'TL export currently supports single-document projects only.')
        document = documents[0]
        return _availability(runtime, document.codec_identity, FormatId(document.format_id))

    def save(self, destination: Path | None = None) -> ProjectPackageExportResult:
        self._require_live()
        binding = self._persistence_binding
        if destination is None:
            if binding is None:
                raise ProjectWorkspaceError('PROJECT.SAVE.VALIDATION_FAILED')
            destination = binding.path
        if binding is None:
            if self._prepared is None:
                raise ProjectWorkspaceError('PROJECT.INTAKE.SOURCE_STALE')
            result = self._prepared.save_workspace(
                self._package_service, self._save_service, destination,
            )
        else:
            result = self._package_service.save_workspace(
                self._save_service, destination, persistence_binding=binding,
            )
        # Project may issue a readback binding while reporting recovery. Keep
        # that owner fact without converting it into a durable success receipt.
        if result.persistence_binding is not None:
            self._persistence_binding = result.persistence_binding
        self._recovery_required = result.save_report.recovery_required
        if ((result.receipt is not None and result.receipt.durable)
                or self._recovery_required):
            if self._prepared is not None:
                self._prepared.close()
                self._prepared = None
        return result

    def open_saved_package(self) -> OpenedProjectPackage:
        """Revalidate the saved package independently of its former source path.

        The immutable returned view describes the saved baseline; unsaved edits
        continue to live only in workspace_service. This call never installs or
        replaces that editing authority.
        """
        self._require_live()
        binding = self._persistence_binding
        if binding is None:
            raise ProjectWorkspaceError('PROJECT.SAVE.VALIDATION_FAILED')
        opened = self._package_service.open(binding.path)
        if opened.persistence_binding != binding:
            raise ProjectWorkspaceError('PROJECT.PACKAGE.SOURCE_STALE')
        return opened

    def close(self) -> None:
        if self._prepared is not None:
            self._prepared.close()
            self._prepared = None
        self._closed = True

    def __enter__(self) -> RpyProjectSession:
        self._require_live()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


class RpyProjectAdapter:
    def __init__(
        self, runtime: ProjectCodecRuntime, *,
        package_service: ProjectPackageService | None = None,
    ) -> None:
        if type(runtime) is not ProjectCodecRuntime:
            raise TypeError('runtime must be exact ProjectCodecRuntime')
        self.runtime = runtime
        self.package_service = package_service if package_service is not None else ProjectPackageService()

    def validate_selected_refs(self, source_refs: tuple[str, ...]) -> int:
        """Bound confirmed TL choices from configured descriptors before body I/O.

        This consumes supplied names only; neither the adapter nor codec scans
        a directory. Other configured formats keep their own intake policy.
        """
        count = 0
        for source_ref in source_refs:
            suffix = PurePosixPath(source_ref).suffix.lower()
            if not suffix:
                continue
            selected = self.runtime.surface.select(SelectionRequest(
                EffectivePurpose.PROJECT_DOCUMENT,
                hints=SelectionHints(extensions=(suffix,))))
            if (type(selected) is CodecDescriptor
                    and selected.identity == _RPY_IDENTITY
                    and selected.format_id == _RPY_FORMAT):
                count += 1
                if count > _MAX_SELECTED_TL:
                    raise RpyProjectSelectionError(
                        'RPY.IMPORT.DOCUMENT_LIMIT_EXCEEDED', source_ref)
            elif suffix == '.rpy':
                # Explain the unavailable known format; never guess a reader.
                availability = _availability(self.runtime, _RPY_IDENTITY, _RPY_FORMAT)
                if not availability.available:
                    raise RpyProjectUnavailableError(availability)
        return count

    def workspace_from_prepared(self, prepared: PreparedSelectedProjectDocuments):
        """Apply TL policy to complete Project-issued neutral intake facts."""
        if type(prepared) is not PreparedSelectedProjectDocuments or prepared.closed:
            raise ProjectWorkspaceError('PROJECT.INTAKE.SOURCE_STALE')
        workspace = prepared.staged.workspace
        documents = []
        language = None
        count = 0
        for document, header in zip(workspace.documents, prepared.verified_headers, strict=True):
            if (document.codec_identity == _RPY_IDENTITY
                    and document.format_id == _RPY_FORMAT.value):
                count += 1
                if count > _MAX_SELECTED_TL:
                    raise RpyProjectSelectionError(
                        'RPY.IMPORT.DOCUMENT_LIMIT_EXCEEDED', document.source_ref)
                if header.target_locale is None or document.codec_private_member is None:
                    raise ProjectWorkspaceError('PROJECT.INTAKE.INPUT_INVALID')
                if language is not None and header.target_locale != language:
                    raise RpyProjectSelectionError('RPY.IMPORT.MIXED_LANGUAGE', document.source_ref)
                language = header.target_locale
                document = replace(document, display_name=PurePosixPath(document.source_ref).name)
            documents.append(document)
        return replace(workspace, documents=tuple(documents))

    def prepare_selected(
        self, root: Path, sources: tuple[Path, ...], request: SelectedProjectDocumentsRequest,
        *, session_id: str, file_system=None, cancellation=None,
    ) -> RpyProjectSession:
        """Retain exactly the caller's ordered explicit files through first save."""
        refs = tuple(source.relative_to(root).as_posix() for source in sources)
        self.validate_selected_refs(refs)
        try:
            prepared = prepare_selected_project_documents(
                root, sources, request, parser_surface=self.runtime.surface,
                file_system=file_system, cancellation=cancellation)
        except SelectedProjectDocumentsError as error:
            raise RpyProjectSelectionError(
                error.code, refs[error.document_order], error.diagnostics) from error
        try:
            workspace = ProjectWorkspaceService(
                self.workspace_from_prepared(prepared), prepared.staged.origin_binding,
                session_id=session_id, revision=0)
            return RpyProjectSession(
                self.package_service, ProjectSaveService(workspace, baseline=None), prepared=prepared)
        except BaseException:
            prepared.close()
            raise

    def prepare_export(
        self, workspace_service: ProjectWorkspaceService,
        persistence_binding: ProjectPackagePersistenceBinding, target: Path, *,
        config_dir: Path, request_generation: int = 0, cancellation=None,
    ):
        """Prepare a display-only preview from the installed Project owner.

        Ordinary package-open services use this same seam as RPY sessions.
        The caller retains the closeable candidate and gives only its view to UI.
        """
        from rpy_project_export import RpyExportContext, prepare_rpy_export

        return prepare_rpy_export(
            RpyExportContext(workspace_service, persistence_binding, self.runtime, request_generation),
            target, config_dir=config_dir, package_service=self.package_service,
            cancellation=cancellation,
        )

    def prepare_single(
        self, root: Path, source: Path, request: SelectedProjectDocumentsRequest,
        *, session_id: str, revision: int = 0, file_system=None, cancellation=None,
        creation_pending: bool = False,
    ) -> RpyProjectSession:
        availability = _availability(self.runtime, _RPY_IDENTITY, _RPY_FORMAT)
        if not availability.available:
            raise RpyProjectUnavailableError(availability)
        if not isinstance(source, Path) or source.suffix.lower() != '.rpy':
            raise ProjectWorkspaceError('PROJECT.INTAKE.INPUT_INVALID')
        prepared = prepare_selected_project_documents(
            root, (source,), request, parser_surface=self.runtime.surface,
            file_system=file_system, cancellation=cancellation,
        )
        try:
            staged = prepared.staged
            _require_single_tl(staged.workspace)
            document = staged.workspace.documents[0]
            named_workspace = replace(staged.workspace, documents=(
                replace(document, display_name=source.name),))
            workspace = ProjectWorkspaceService(
                named_workspace, staged.origin_binding,
                session_id=session_id, revision=revision,
            )
            return RpyProjectSession(
                self.package_service, ProjectSaveService(workspace, baseline=None),
                prepared=prepared,
                creation_pending=creation_pending,
            )
        except BaseException:
            prepared.close()
            raise

    def open_package(
        self, source: Path, *, session_id: str, revision: int = 0,
    ) -> RpyProjectSession:
        # Package validation and neutral persistence do not consult the codec.
        opened = self.package_service.open(source)
        _require_single_tl(opened.workspace)
        return RpyProjectSession(
            self.package_service,
            opened.create_save_service(session_id=session_id, revision=revision),
            persistence_binding=opened.persistence_binding,
        )
