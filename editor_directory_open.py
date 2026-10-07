"""Qt-free lifetime of one explicit directory discovery and first publication."""
from threading import RLock
from uuid import uuid4

from editor_file_jobs import DirectoryImportReview, FileImportPublication, FileOpenCandidate
from platform_fs import compose_platform_file_backend
from project_codec_settings import compose_project_codec_runtime
from project_directory_contracts import DirectorySelectionRequest
from project_save import ProjectSaveService
from project_workspace import ProjectWorkspaceService
from project_workspace_discovery import ProjectDirectoryDiscoveryService
from project_workspace_identity import ProjectWorkspaceError
from project_workspace_intake import prepare_directory_project_documents
from rpy_project_adapter import RpyProjectAdapter


class DirectoryInputError(ValueError):
    """Controller-safe context around an owner diagnostic, without source text."""
    def __init__(self, error, source_ref):
        self.code = getattr(error, 'code', 'PROJECT.INTAKE.INPUT_INVALID')
        self.diagnostics = getattr(error, 'diagnostics', ())
        self.source_ref = source_ref
        super().__init__(self.code)


class DirectoryOpenCandidate:
    """Cancellation revokes discovery; worker cleanup alone releases live intake."""
    def __init__(self, root, config_dir, package_service, file_system=None):
        self.root = root
        self.config_dir = config_dir
        self.package_service = package_service
        self.file_system = file_system
        self._lock = RLock()
        self._cancelled = False
        self.discovery = None
        self.runtime = None
        self.review = None

    def cancel(self):
        with self._lock:
            self._cancelled = True
            if self.discovery is not None:
                self.discovery.cancel()

    def close(self):
        with self._lock:
            self._cancelled = True
            if self.discovery is not None:
                self.discovery.close()

    def preview(self, cancellation):
        try:
            runtime = compose_project_codec_runtime(self.config_dir)
            backend = self.file_system or compose_platform_file_backend(self.root)
            discovery = ProjectDirectoryDiscoveryService(backend, runtime.surface)
            with self._lock:
                cancellation.raise_if_cancelled()
                if self._cancelled:
                    discovery.close()
                    raise ProjectWorkspaceError('PROJECT.DIRECTORY.CANCELLED')
                self.discovery, self.runtime = discovery, runtime
            preview = discovery.preview(self.root)
            cancellation.raise_if_cancelled()
            self.review = DirectoryImportReview(preview, self.root.name or 'project')
            return self
        except BaseException:
            self.close()
            raise

    def publish(self, entry_ids, request, destination, cancellation):
        prepared = None
        try:
            cancellation.raise_if_cancelled()
            preview = self.review.preview
            selected = self.discovery.select(DirectorySelectionRequest(
                preview.preview_id, preview.generation, entry_ids))
            retained = self.discovery.revalidate(selected)
            # Only original preview facts are used for diagnostics after publication.
            by_id = {entry.entry_id: entry.source_ref for entry in preview.entries}
            refs = tuple(by_id[entry_id] for entry_id in entry_ids)
            adapter = RpyProjectAdapter(self.runtime, package_service=self.package_service)
            adapter.validate_selected_refs(refs)
            try:
                prepared = prepare_directory_project_documents(
                    retained, request, parser_surface=self.runtime.surface,
                    file_system=self.file_system, cancellation=cancellation)
            except Exception as error:
                order = getattr(error, 'document_order', None)
                if type(order) is int and 0 <= order < len(refs):
                    raise DirectoryInputError(error, refs[order]) from error
                raise
            staged = prepared.staged
            workspace = ProjectWorkspaceService(adapter.workspace_from_prepared(prepared), staged.origin_binding,
                session_id=uuid4().hex, revision=0)
            save = ProjectSaveService(workspace, baseline=None)
            cancellation.raise_if_cancelled()
            result = prepared.save_workspace(self.package_service, save, destination)
            candidate = FileOpenCandidate(save, result.persistence_binding, runtime=self.runtime)
            return FileImportPublication(candidate, result)
        finally:
            if prepared is not None:
                prepared.close()
            self.close()
