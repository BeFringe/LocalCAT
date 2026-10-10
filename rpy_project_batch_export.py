"""Application batch coordinator; each file retains its Parser publication result."""
from dataclasses import replace
from pathlib import Path, PurePath
from threading import RLock
from uuid import uuid4

from parser_composition import RoundTripPreparationError
from platform_export_directory import ExportDirectoryBatch
from platform_fs import compose_platform_file_backend
from project_export_contracts import (
    ProjectExportBatchView, ProjectExportBatchResult, ProjectExportResult,
    ProjectExportDirectoryResult, ProjectExportDirectoryFailure,
)
from rpy_project_export import (
    PreparedRpyProjectExport, RpyExportContext, _TargetObservation, _checkpoint, _fail,
)


class _PlanObservation:
    """Borrowed plan; only the enclosing batch closes the shared authorities."""
    def __init__(self, batch, plan, backend):
        self.batch, self.plan, self.backend = batch, plan, backend
        self.expected = plan.expected

    def reprove(self):
        self.batch.reprove(self.plan)

    def reject_same_file(self, path):
        if self.expected is not None:
            with self.backend.bind_root(path.parent) as root:
                with self.backend.open_regular(root, PurePath(path.name)) as source:
                    if source.snapshot().identity == self.expected.identity:
                        _fail('RPY.EXPORT.TARGET_INVALID')

    def close(self):
        pass


def _unattempted(view):
    return ProjectExportResult(
        view.preview_id, view.session_id, view.workspace_revision, view.request_generation,
        view.document_id, view.source_ref, view.target_path, 'not_attempted')


class PreparedRpyBatchExport:
    """One confirmation-bound batch. Directory preparation always consumes it."""
    def __init__(self, context, target, indices, config_dir, package_service, cancellation):
        self._context, self._target, self._cancellation = context, target, cancellation
        self._root = self._directory_batch = None
        self._lock = RLock()
        self._closed = False
        self._result = self._directory_result = None
        self._candidates = tuple(PreparedRpyProjectExport(
            context, target.joinpath(*context.workspace_service.workspace.documents[index].source_ref.split('/')),
            config_dir, package_service, cancellation, document_index=index) for index in indices)
        self._view = ProjectExportBatchView(
            uuid4().hex, context.workspace_service.session_id, context.workspace_service.revision,
            context.request_generation, str(target), tuple(c.view for c in self._candidates), (), 'preparing')
        self._results = [_unattempted(view) for view in self._view.files]

    @property
    def view(self):
        return self._view

    @property
    def closed(self):
        return self._closed

    @property
    def terminal_result(self):
        """Read-only terminal facts, including after interrupted worker cleanup."""
        return self._result or self._directory_result

    def _diagnostic(self, code, summary):
        return self._candidates[0]._diagnostic(code, summary)

    def _prepare(self):
        current = self._candidates[0]
        try:
            _checkpoint(self._cancellation)
            backend = compose_platform_file_backend(self._target)
            self._root = backend.bind_root(self._target)
            self._directory_batch = ExportDirectoryBatch(backend, self._root)
            missing = []
            # All names are checked before any output content is prepared.
            for current in self._candidates:
                plan = self._directory_batch.prepare_descendant_target(current.view.source_ref)
                current._directory_binding = (self._directory_batch, plan)
                current._target_observation = _PlanObservation(self._directory_batch, plan, backend)
                parts = plan.relative_path.split('/')[:-1]
                for length in range(len(parts) - len(plan.missing_directories) + 1, len(parts) + 1):
                    path = '/'.join(parts[:length])
                    if path not in missing:
                        missing.append(path)
            self._view = replace(self._view, missing_directories=tuple(missing))
            for current in self._candidates:
                current._prepare()
                if current.view.status != 'ready':
                    self._stop(current.view.status)
                    return
            self._require_current(self._context, self._target)
            self._view = replace(self._view, status='ready', files=tuple(c.view for c in self._candidates),
                diagnostics=tuple(d for c in self._candidates for d in c.view.diagnostics))
        except RoundTripPreparationError as error:
            current._stop('blocked', error.code, tuple(
                current._diagnostic(issue.code, issue.safe_summary, issue) for issue in error.diagnostics))
            self._stop('blocked')
        except Exception as error:
            code = getattr(error, 'code', 'RPY.EXPORT.PREPARATION_FAILED')
            status = 'cancelled' if code == 'PARSER.SOURCE.CANCELLED' else 'blocked'
            current._stop(status, code)
            self._stop(status)
        except BaseException:
            self.close()
            raise

    def _stop(self, status):
        self._view = replace(self._view, status=status, files=tuple(c.view for c in self._candidates),
            diagnostics=tuple(d for c in self._candidates for d in c.view.diagnostics))
        try:
            self.close()
        except Exception:
            self._view = replace(self._view, diagnostics=self._view.diagnostics + (self._diagnostic(
                'RPY.EXPORT.CLEANUP_FAILED', 'Export resource cleanup could not be proved.'),))

    def _require_current(self, context, target):
        if target != self._target or self._closed:
            _fail('RPY.EXPORT.PREVIEW_STALE')
        for candidate in self._candidates:
            candidate._require_current(context, candidate._target)

    def revalidate(self, context, target):
        with self._lock:
            if not self._closed:
                try:
                    self._require_current(context, target)
                except Exception as error:
                    status = 'cancelled' if getattr(error, 'code', '') == 'PARSER.SOURCE.CANCELLED' else 'stale'
                    self._view = replace(self._view, diagnostics=(self._diagnostic(
                        getattr(error, 'code', 'RPY.EXPORT.PREVIEW_STALE'), 'The batch preview is no longer usable.'),))
                    # Preserve the rejection diagnostic while closing candidates.
                    self._view = replace(self._view, status=status)
                    self.close()
                except BaseException:
                    self.close()
                    raise
            return self._view

    def prepare_directories(self, context, target):
        with self._lock:
            if self._directory_result is not None:
                return self._directory_result
            outcome, diagnostics = 'failed', ()
            try:
                self._require_current(context, target)
                if self._view.status != 'ready' or not self._view.missing_directories:
                    _fail('RPY.EXPORT.PREVIEW_STALE')
                self._directory_batch.prepare_directories(self._cancellation)
                outcome = 'prepared'
            except Exception as error:
                code = getattr(error, 'code', 'RPY.EXPORT.DIRECTORY_FAILED')
                outcome = 'cancelled' if code == 'PARSER.SOURCE.CANCELLED' else 'failed'
                diagnostics = (self._diagnostic(code, 'Directory preparation did not complete.'),)
            finally:
                batch = self._directory_batch
                created = batch.created_directories if batch is not None else ()
                failures = tuple(ProjectExportDirectoryFailure(f.relative_path, f.outcome, f.code)
                                 for f in batch.directory_failures) if batch is not None else ()
                interruption = None
                try:
                    self.close()
                except BaseException as error:
                    outcome = 'uncertain'
                    diagnostics += (self._diagnostic('RPY.EXPORT.CLEANUP_FAILED',
                        'Directory preparation cleanup could not be proved.'),)
                    if not isinstance(error, Exception):
                        interruption = error
                view = self._view
                self._directory_result = ProjectExportDirectoryResult(
                    view.preview_id, view.session_id, view.workspace_revision, view.request_generation,
                    view.target_path, outcome, created, failures, diagnostics)
                if interruption is not None:
                    raise interruption
            return self._directory_result

    def _bind(self, candidate):
        candidate._require_current(self._context, candidate._target)
        batch, plan = candidate._directory_binding
        observation = _TargetObservation(candidate._target)
        materialized = None
        try:
            if observation.expected != plan.expected:
                _fail('RPY.EXPORT.TARGET_STALE')
            materialized = batch.materialize_target(plan, self._cancellation)
            self._context.runtime.surface.bind_materialized_round_trip_target(candidate._prepared, materialized)
            candidate._target_observation = observation
            observation = None
        finally:
            if materialized is not None:
                materialized.close()
            if observation is not None:
                observation.close()

    def publish(self, context, target):
        with self._lock:
            if self._result is not None:
                return self._result
            if self._closed:
                outcome = 'not_attempted' if self._directory_result else self._view.status
                return self._finish(outcome, self._view.diagnostics)
            if self._view.missing_directories:
                return self._finish('blocked', (self._diagnostic('RPY.EXPORT.DIRECTORIES_REQUIRED',
                    'Prepare the missing directories, then confirm a new preview.'),))
            try:
                self._require_current(context, target)
            except Exception as error:
                code = getattr(error, 'code', 'RPY.EXPORT.PREVIEW_STALE')
                return self._finish('cancelled' if code == 'PARSER.SOURCE.CANCELLED' else 'stale',
                    (self._diagnostic(code, 'The batch preview is no longer usable.'),))
            except BaseException:
                self._finish('not_attempted', (self._diagnostic('RPY.EXPORT.INTERRUPTED',
                    'Validation was interrupted before publication.'),))
                raise
            diagnostics = ()
            outcome = 'published'
            try:
                for index, candidate in enumerate(self._candidates):
                    try:
                        _checkpoint(self._cancellation)
                        candidate._require_current(context, candidate._target)
                    except Exception as error:
                        code = getattr(error, 'code', 'RPY.EXPORT.PREVIEW_STALE')
                        outcome = 'cancelled' if code == 'PARSER.SOURCE.CANCELLED' else 'stale'
                        diagnostics = (candidate._diagnostic(code, 'Remaining files were not attempted.'),)
                        break
                    try:
                        self._bind(candidate)
                    except Exception as error:
                        code = getattr(error, 'code', 'RPY.EXPORT.TARGET_FAILED')
                        if code == 'PARSER.SOURCE.CANCELLED':
                            outcome = 'cancelled'
                            break
                        self._results[index] = candidate._finish('failed', (candidate._diagnostic(
                            code, 'The file could not be bound for publication.'),))
                    else:
                        result = candidate.publish(context, candidate._target)
                        if result.outcome in ('cancelled', 'stale', 'blocked'):
                            result = replace(result, outcome='not_attempted')
                        self._results[index] = result
                    if self._results[index].outcome != 'published':
                        outcome = self._results[index].outcome
                        break
            except BaseException:
                # The per-file publisher caches interrupted dispatch outcomes.
                for index, candidate in enumerate(self._candidates):
                    if candidate._result is not None:
                        self._results[index] = candidate._result
                self._finish('uncertain', (self._diagnostic('RPY.EXPORT.INTERRUPTED',
                    'The batch was interrupted; inspect the individual file results.'),))
                raise
            if outcome != 'published' and any(r.outcome == 'published' for r in self._results):
                outcome = 'partial'
            return self._finish(outcome, diagnostics)

    def _finish(self, outcome, diagnostics):
        view = self._view
        self._result = ProjectExportBatchResult(
            view.preview_id, view.session_id, view.workspace_revision, view.request_generation,
            view.target_path, tuple(self._results), outcome,
            tuple(d for result in self._results for d in result.diagnostics) + diagnostics)
        try:
            self.close()
        except BaseException as error:
            diagnostics += (self._diagnostic('RPY.EXPORT.CLEANUP_FAILED',
                'Batch cleanup could not be proved; individual publication facts are retained.'),)
            if outcome == 'published':
                outcome = 'uncertain'
            self._result = replace(self._result, outcome=outcome,
                diagnostics=tuple(d for result in self._results for d in result.diagnostics) + diagnostics)
            if not isinstance(error, Exception):
                raise
        finally:
            self._view = replace(view, status=outcome)
        return self._result

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._view.status in ('ready', 'preparing'):
                self._view = replace(self._view, status='cancelled')
            failure = None
            for authority in (*self._candidates, self._directory_batch, self._root):
                if authority is not None:
                    try:
                        authority.close()
                    except BaseException as error:
                        failure = failure or error
            if failure is not None:
                raise failure


def prepare_batch(context, target, *, document_ids, config_dir, package_service, cancellation=None):
    if type(context) is not RpyExportContext:
        raise TypeError('context must be exact RpyExportContext')
    if not isinstance(target, Path) or not target.is_absolute() or '..' in target.parts:
        raise ValueError('target must be an absolute Path without parent traversal')
    if not isinstance(config_dir, Path) or not config_dir.is_absolute():
        raise ValueError('config_dir must be an absolute Path')
    documents = context.workspace_service.workspace.documents
    eligible = {document.document_id for document in documents
                if document.codec_identity.provider_id == 'localcat.rpy'}
    if (type(document_ids) is not tuple or not 0 < len(document_ids) <= 256
            or any(type(value) is not str for value in document_ids)
            or len(set(document_ids)) != len(document_ids) or not set(document_ids) <= eligible):
        raise ValueError('select unique current Ren’Py TL documents')
    indices = tuple(index for index, document in enumerate(documents) if document.document_id in document_ids)
    candidate = PreparedRpyBatchExport(context, target, indices, config_dir, package_service, cancellation)
    try:
        candidate._prepare()
    except BaseException:
        candidate.close()
        raise
    return candidate
