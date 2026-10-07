"""Single-TL export coordination over public Project and Parser seams.

Current edits remain Project-owned. A preview owns a live Parser preparation,
its temporary verified source and read-only target observation until discarded.
The display DTO grants no publication capability. Callers must revalidate with
the currently installed context after asynchronous work and before consumption.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path, PurePath
import tempfile
from threading import RLock
from uuid import uuid4

from parser_composition import RoundTripPreparationError
from parser_contracts import (
    ContractViolation, EffectivePurpose, FormatId, ParsedSegment, ReadRequest,
    RoundTripSegmentEdit, RoundTripTokenEnvelope, SelectionFailure,
    SelectionRequest, SourceReference, TargetReference,
)
from platform_fs import compose_platform_file_backend
from platform_fs_contracts import PlatformFileError, PublishMode
from project_codec_settings import (
    CodecSettingsError, CodecSettingsRepository, ProjectCodecRuntime,
)
from project_export_contracts import ProjectExportDiagnostic, ProjectExportResult, ProjectExportView
from project_package import ProjectPackagePersistenceBinding, ProjectPackageService
from project_workspace import ProjectWorkspaceService
from project_workspace_contracts import ProjectWorkspaceError, SourcePresence


_CHUNK = 64 * 1024
_MAX_SOURCE = 16 * 1024 * 1024
_MAX_PRIVATE = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class RpyExportContext:
    """Application input; works with ordinary Controller package-open services.

    The Controller owns issuance, installed-session membership and generation.
    This context is never passed to Qt or to a codec.
    """

    workspace_service: ProjectWorkspaceService
    persistence_binding: ProjectPackagePersistenceBinding
    runtime: ProjectCodecRuntime
    request_generation: int = 0

    def __post_init__(self):
        if type(self.workspace_service) is not ProjectWorkspaceService:
            raise TypeError('workspace_service must be exact ProjectWorkspaceService')
        if type(self.persistence_binding) is not ProjectPackagePersistenceBinding:
            raise TypeError('persistence_binding must be exact ProjectPackagePersistenceBinding')
        if type(self.runtime) is not ProjectCodecRuntime:
            raise TypeError('runtime must be exact ProjectCodecRuntime')
        if type(self.request_generation) is not int or self.request_generation < 0:
            raise ValueError('request_generation must be a nonnegative integer')


class RpyExportError(ValueError):
    """Body-safe failure of Application export coordination."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise RpyExportError(code)


def _checkpoint(cancellation):
    if cancellation is not None:
        cancellation.raise_if_cancelled()


def _member_chunks(stream, reference, maximum, cancellation):
    if reference.byte_count > maximum:
        _fail('PROJECT.WORKSPACE.LIMIT_EXCEEDED')
    digest = hashlib.sha256()
    count = 0
    while True:
        _checkpoint(cancellation)
        chunk = stream.read(_CHUNK)
        if not chunk:
            break
        count += len(chunk)
        if count > reference.byte_count or count > maximum:
            _fail('PROJECT.PACKAGE.MEMBER_INVALID')
        digest.update(chunk)
        yield chunk
    if count != reference.byte_count or digest.hexdigest() != reference.sha256:
        _fail('PROJECT.PACKAGE.DIGEST_MISMATCH')


class _SourceBridge:
    """Owned temporary regular source; no package/private authority escapes."""

    def __init__(self, config_dir):
        config_dir.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix='rpy-export-', dir=config_dir))
        self.path = self.directory / 'source.rpy'
        self.root = self.parent = None
        self._owned_names = {}
        try:
            self.backend = compose_platform_file_backend(self.directory)
        except BaseException:
            self.directory.rmdir()
            raise

    def copy(self, package, reference, cancellation):
        candidate = pending = None
        candidate_identity = None
        candidate_name = 'source-candidate.tmp'
        try:
            self.root = self.backend.bind_root(self.directory)
            self.parent = self.backend.bind_parent(self.root, PurePath(self.path.name))
            candidate = self.parent.create_candidate(candidate_name, private=True)
            candidate_identity = candidate.identity()
            self._owned_names[candidate_name] = candidate_identity
            # Context exit reproof must succeed before publishing even this
            # private bridge, and before any Parser consumer sees the member.
            with package.open_member(reference.path) as stream:
                facts = candidate.write_chunks(
                    _member_chunks(stream, reference, _MAX_SOURCE, cancellation),
                    maximum_bytes=_MAX_SOURCE,
                )
            _checkpoint(cancellation)
            candidate.flush_content()
            # If the platform raises after rename but before returning its
            # pending handle, cleanup still knows the identity it may remove.
            self._owned_names[self.path.name] = candidate_identity
            pending = self.parent.begin_publish(
                candidate, self.path.name, mode=PublishMode.CREATE_IF_ABSENT, lease=None,
            )
            candidate = None
            candidate_identity = None
            preliminary = pending.preliminary_facts()
            self._owned_names[self.path.name] = preliminary.destination_identity
            retained = pending.retained_destination()
            snapshot = retained.snapshot()
            digest = hashlib.sha256()
            for chunk in retained.iter_exact_chunks(snapshot):
                _checkpoint(cancellation)
                digest.update(chunk)
            if (facts.content_sha256.hex() != reference.sha256
                    or facts.byte_count != reference.byte_count
                    or preliminary.content_sha256 != facts.content_sha256
                    or preliminary.byte_count != facts.byte_count
                    or snapshot.byte_count != facts.byte_count
                    or digest.digest() != facts.content_sha256
                    or pending.terminal_reproof() != preliminary):
                _fail('PROJECT.PACKAGE.DIGEST_MISMATCH')
        finally:
            # Windows Parser source open must see no candidate/publication or
            # retained destination writer handles from the private copy.
            for authority in (pending, candidate):
                if authority is not None:
                    authority.close()

    def close(self):
        try:
            if self.parent is not None:
                for name, identity in self._owned_names.items():
                    observed = self.parent.inspect_entry(name)
                    if observed is not None:
                        self.parent.unlink_owned(name, identity)
                self._owned_names.clear()
        finally:
            for authority in (self.parent, self.root):
                if authority is not None:
                    authority.close()
            self.parent = self.root = None
            # Never recursively delete a replaced name or an unowned entry.
            try:
                self.directory.rmdir()
            except FileNotFoundError:
                pass


class _TargetObservation:
    """Read-only stale detection, separate from Parser's publication authority."""

    def __init__(self, path):
        self.path = path
        self.root = self.parent = None
        try:
            self.backend = compose_platform_file_backend(path.parent)
            self.root = self.backend.bind_root(path.parent)
            self.parent = self.backend.bind_parent(self.root, PurePath(path.name))
            self.expected = self.parent.inspect_entry(path.name)
            self.reprove()
        except BaseException:
            self.close()
            raise

    def reprove(self):
        self.root.reprove()
        self.parent.reprove()
        if self.parent.inspect_entry(self.path.name) != self.expected:
            _fail('RPY.EXPORT.TARGET_STALE')

    def reject_same_file(self, path):
        if self.expected is None:
            return
        with self.backend.bind_root(path.parent) as root:
            with self.backend.open_regular(root, PurePath(path.name)) as source:
                if source.snapshot().identity == self.expected.identity:
                    _fail('RPY.EXPORT.TARGET_INVALID')

    def close(self):
        for authority in (self.parent, self.root):
            if authority is not None:
                authority.close()
        self.parent = self.root = None


class PreparedRpyProjectExport:
    """Closeable Application candidate, retained by Controller, never by Qt.

    Only Parser owns the prepared write. Revalidation requires current owner
    inputs; an old context alone cannot prove that a project is still installed.
    Publication consumes this candidate after revalidation; no public token,
    bytes, or raw writer getter is provided. Operations are synchronous worker
    operations and serialize with close; cancellation uses the supplied token.
    """

    def __init__(self, context, target, config_dir, package_service, cancellation):
        self._context = context
        self._workspace = context.workspace_service.workspace
        self._revision = context.workspace_service.revision
        self._config_dir = config_dir
        self._package_service = package_service
        self._cancellation = cancellation
        self._target = target
        self._bridge = self._opened = self._prepared = self._target_observation = None
        self._closed = False
        self._operation_lock = RLock()
        self._result = None
        document = self._workspace.documents[0]
        self._document = document
        self._records = ()
        attached_segments = tuple(segment for segment, source in zip(
            document.segments, document.source_segments, strict=True)
            if source.source_presence is SourcePresence.ATTACHED)
        self._view = ProjectExportView(
            uuid4().hex, context.workspace_service.session_id, self._revision,
            context.request_generation, document.document_id, document.source_ref,
            str(target), len(attached_segments), None,
            sum(segment.target == '' for segment in attached_segments),
            sum(not segment.confirmed for segment in attached_segments), 'preparing',
        )

    @property
    def view(self) -> ProjectExportView:
        return self._view

    @property
    def closed(self) -> bool:
        return self._closed

    def _diagnostic(self, code, summary, issue=None):
        local_id = None
        if issue is not None and issue.record_number is not None:
            index = issue.record_number - 1
            if 0 <= index < len(self._records):
                local_id = self._records[index].local_id
        return ProjectExportDiagnostic(
            code, issue.severity.value if issue is not None else 'fatal', summary,
            self._document.document_id, self._document.source_ref, local_id,
            issue.line_number if issue is not None else None,
            issue.byte_offset if issue is not None else None,
        )

    def _stop(self, status, code, diagnostics=()):
        self._view = replace(
            self._view, status=status,
            diagnostics=diagnostics or (self._diagnostic(code, 'Export preparation is no longer usable.'),),
        )
        try:
            self.close()
        except Exception:
            self._view = replace(self._view, diagnostics=self._view.diagnostics + (
                self._diagnostic('RPY.EXPORT.CLEANUP_FAILED',
                                 'Export resource cleanup could not be proved.'),))
        return self._view

    def _require_context(self, context, target):
        _checkpoint(self._cancellation)
        original = self._context
        if (type(context) is not RpyExportContext
                or context.workspace_service is not original.workspace_service
                or context.workspace_service.workspace is not self._workspace
                or context.workspace_service.revision != self._revision
                or context.request_generation != original.request_generation
                or context.persistence_binding != original.persistence_binding
                or context.runtime.settings != original.runtime.settings
                or context.runtime.availability != original.runtime.availability
                or target != self._target):
            _fail('RPY.EXPORT.PREVIEW_STALE')
        selection = SelectionRequest(EffectivePurpose.PROJECT_DOCUMENT, FormatId(self._document.format_id))
        if context.runtime.surface.select(selection) != original.runtime.surface.select(selection):
            _fail('RPY.EXPORT.PREVIEW_STALE')
        if CodecSettingsRepository(self._config_dir).load() != original.runtime.settings:
            _fail('RPY.EXPORT.PREVIEW_STALE')

    def _require_current(self, context, target):
        self._require_context(context, target)
        original = self._context
        package = self._package_service.open(original.persistence_binding.path)
        if package.persistence_binding != original.persistence_binding:
            _fail('PROJECT.PACKAGE.SOURCE_STALE')
        if self._target_observation is not None:
            self._target_observation.reprove()
        if self._prepared is not None and self._prepared.closed:
            _fail('RPY.EXPORT.PREVIEW_STALE')
        # Package validation can take time. Recheck edits, cancellation and
        # configuration after I/O as well as before it.
        self._require_context(context, target)
        return package

    def revalidate(self, context: RpyExportContext, target: Path) -> ProjectExportView:
        """Worker operation: reread the package and reject stale/late results.

        This performs I/O and must not be polled from the GUI event thread.
        """
        with self._operation_lock:
            if self._closed:
                return self._view
            try:
                self._require_current(context, target)
            except (ContractViolation, ProjectWorkspaceError, RpyExportError, PlatformFileError, CodecSettingsError, OSError) as error:
                code = getattr(error, 'code', 'RPY.EXPORT.PREVIEW_STALE')
                return self._stop('cancelled' if code == 'PARSER.SOURCE.CANCELLED' else 'stale', code)
            return self._view

    def publish(self, context: RpyExportContext, target: Path) -> ProjectExportResult:
        """Revalidate and consume once on a worker, using the issuing surface.

        A repeated call returns the same terminal result without writing. The
        Controller must supply its current context and selected target. Parser
        outcomes remain authoritative after dispatch, including cancellation
        during an issued operation; no cancellation checkpoint follows it.
        """
        with self._operation_lock:
            if self._result is not None:
                return self._result
            if self._closed:
                return self._finish(self._view.status, self._view.diagnostics)
            try:
                self._require_current(context, target)
            except (ContractViolation, ProjectWorkspaceError, RpyExportError, PlatformFileError, CodecSettingsError, OSError) as error:
                code = getattr(error, 'code', 'RPY.EXPORT.PREVIEW_STALE')
                return self._finish(
                    'cancelled' if code == 'PARSER.SOURCE.CANCELLED' else 'stale',
                    (self._diagnostic(code, 'Export preparation is no longer usable.'),),
                )
            except Exception:
                return self._finish('failed', (self._diagnostic(
                    'RPY.EXPORT.VALIDATION_FAILED', 'Export validation failed before publication.'),))
            except BaseException:
                self.close()
                raise
            try:
                result = self._context.runtime.surface.write_prepared(self._prepared)
            except Exception:
                # Once dispatched, only the Parser can prove non-publication.
                return self._finish('uncertain', (self._diagnostic(
                    'PARSER.SOURCE.WRITE_RECOVERY_REQUIRED',
                    'Publication requires inspection; no success was proved.'),))
            except BaseException:
                self._finish('uncertain', (self._diagnostic(
                    'PARSER.SOURCE.WRITE_RECOVERY_REQUIRED',
                    'Publication was interrupted and requires inspection.'),))
                raise
            diagnostics = self._view.diagnostics
            if result.code is not None:
                diagnostics += (self._diagnostic(result.code, result.safe_summary),)
            receipt = result.receipt
            return self._finish(
                result.outcome.value, diagnostics,
                receipt.content_sha256 if receipt is not None else None,
                receipt.byte_count if receipt is not None else None,
            )

    def _finish(self, outcome, diagnostics, output_sha256=None, output_byte_count=None):
        self._view = replace(self._view, status=outcome, diagnostics=diagnostics)
        try:
            self.close()
        except Exception:
            diagnostics += (self._diagnostic(
                'RPY.EXPORT.CLEANUP_FAILED', 'Export resource cleanup could not be proved.'),)
            if outcome == 'published':
                outcome = 'uncertain'
                output_sha256 = output_byte_count = None
            self._view = replace(self._view, status=outcome, diagnostics=diagnostics)
        view = self._view
        self._result = ProjectExportResult(
            view.preview_id, view.session_id, view.workspace_revision,
            view.request_generation, view.document_id, view.source_ref,
            view.target_path, outcome, diagnostics, output_sha256, output_byte_count,
        )
        return self._result

    def close(self):
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            if self._view.status in ('ready', 'preparing'):
                self._view = replace(self._view, status='cancelled')
            authorities = (self._prepared, self._opened, self._target_observation, self._bridge)
            self._prepared = self._opened = self._bridge = self._target_observation = None
            failure = None
            for authority in authorities:
                if authority is None:
                    continue
                try:
                    authority.close()
                except BaseException as error:
                    if failure is None:
                        failure = error
            if failure is not None:
                raise failure

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _prepare(self):
        # Importing the adapter here avoids a cycle with its public factory.
        from rpy_project_adapter import _availability, _require_single_tl

        context = self._context
        document = self._document
        _checkpoint(self._cancellation)
        _require_single_tl(self._workspace)
        format_id = FormatId(document.format_id)
        available = _availability(context.runtime, document.codec_identity, format_id)
        if not available.available:
            return self._stop('blocked', available.code, (
                self._diagnostic(available.code, available.safe_summary),))
        package = self._require_current(context, self._target)
        saved = package.workspace.documents[0]
        if (package.workspace.project_id != self._workspace.project_id
                or package.workspace.origin != self._workspace.origin
                or saved.document_id != document.document_id
                or saved.source_ref != document.source_ref
                or saved.codec_identity != document.codec_identity
                or saved.format_id != document.format_id
                or saved.source_snapshot_digest != document.source_snapshot_digest
                or saved.codec_private_member != document.codec_private_member
                or saved.source_segments != document.source_segments):
            _fail('PROJECT.PACKAGE.SOURCE_STALE')
        entry = package.manifest.documents[0]
        private_ref = document.codec_private_member
        if (entry.source_member.sha256 != document.source_snapshot_digest
                or entry.codec_private_member is None
                or entry.codec_private_member.path != private_ref.member_path
                or entry.codec_private_member.sha256 != private_ref.sha256
                or entry.codec_private_member.byte_count != private_ref.byte_count
                or private_ref.profile_version != 'rpy-roundtrip-v1'):
            _fail('PROJECT.PACKAGE.MEMBER_INVALID')
        if self._target == context.persistence_binding.path or self._target.is_relative_to(self._config_dir):
            _fail('RPY.EXPORT.TARGET_INVALID')
        self._bridge = _SourceBridge(self._config_dir)
        self._bridge.copy(package, entry.source_member, self._cancellation)
        # Do not retain bytes until the full private member context has exited.
        with package.open_member(entry.codec_private_member.path,
                                 codec_identity=document.codec_identity) as stream:
            private = b''.join(_member_chunks(stream, entry.codec_private_member, _MAX_PRIVATE, self._cancellation))
        surface = context.runtime.surface
        opened = surface.open_input(
            SourceReference(str(self._bridge.directory), str(self._bridge.path), 'Ren\'Py TL source'),
            SelectionRequest(EffectivePurpose.PROJECT_DOCUMENT, format_id),
            ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, format_id),
            cancellation=self._cancellation,
        )
        if type(opened) is SelectionFailure:
            _fail(opened.code)
        self._opened = opened
        if (opened.descriptor.identity != document.codec_identity
                or opened.descriptor.format_id != format_id
                or opened.source_identity.content_sha256 != entry.source_member.sha256
                or opened.source_identity.byte_count != entry.source_member.byte_count):
            _fail('PROJECT.PACKAGE.SOURCE_STALE')
        parsed = opened.materialize()
        records = parsed.records
        attached_sources = tuple(source for source in document.source_segments
                                 if source.source_presence is SourcePresence.ATTACHED)
        if (len(records) != len(attached_sources)
                or any(type(record) is not ParsedSegment
                       or record.local_id != source.local_segment_id
                       or record.source != source.source
                       or record.speaker.value != source.raw_speaker
                       for record, source in zip(records, attached_sources))):
            _fail('RPY.EXPORT.SOURCE_MISMATCH')
        self._records = records
        segments = tuple(segment for segment, source in zip(
            document.segments, document.source_segments, strict=True)
            if source.source_presence is SourcePresence.ATTACHED)
        self._view = replace(self._view, modified_count=sum(
            segment.target != record.target for segment, record in zip(segments, records, strict=True)))
        self._prepared = surface.prepare_round_trip(
            opened, RoundTripTokenEnvelope(
                document.codec_identity, entry.source_member.sha256,
                entry.codec_private_member.sha256, private,
            ),
            tuple(RoundTripSegmentEdit(segment.identity.local_segment_id, segment.target) for segment in segments),
        )
        # Observe before Parser binding and reprove afterwards, so a target
        # race between these two readers cannot silently become a new preview.
        self._target_observation = _TargetObservation(self._target)
        self._target_observation.reject_same_file(context.persistence_binding.path)
        self._target_observation.reject_same_file(self._bridge.path)
        surface.bind_round_trip_target(self._prepared, TargetReference(
            str(self._target.parent), str(self._target), self._target.name))
        # Parser retains sealed bytes, not this physical source. Keeping
        # the bridge's rooted directory chain would lock device settings
        # against replacement on Windows for the whole preview lifetime.
        self._bridge.close()
        self._bridge = None
        self._require_current(context, self._target)
        self._view = replace(
            self._view, status='ready', output_sha256=self._prepared.output_fingerprint,
            output_byte_count=self._prepared.byte_count,
            diagnostics=tuple(self._diagnostic(issue.code, issue.safe_summary, issue)
                              for issue in self._prepared.diagnostics),
        )
        return self._view


def prepare_rpy_export(
    context: RpyExportContext, target: Path, *, config_dir: Path,
    package_service: ProjectPackageService, cancellation=None,
) -> PreparedRpyProjectExport:
    """Prepare all current targets without writing the selected destination."""
    if type(context) is not RpyExportContext:
        raise TypeError('context must be exact RpyExportContext')
    if not isinstance(target, Path) or not target.is_absolute() or '..' in target.parts:
        raise ValueError('target must be an absolute Path without parent traversal')
    if not isinstance(config_dir, Path) or not config_dir.is_absolute():
        raise ValueError('config_dir must be an absolute Path')
    candidate = PreparedRpyProjectExport(context, target, config_dir, package_service, cancellation)
    try:
        candidate._prepare()
    except RoundTripPreparationError as error:
        candidate._stop('blocked', error.code, tuple(
            candidate._diagnostic(issue.code, issue.safe_summary, issue) for issue in error.diagnostics))
    except (ContractViolation, ProjectWorkspaceError, RpyExportError, PlatformFileError, CodecSettingsError, OSError) as error:
        code = getattr(error, 'code', 'RPY.EXPORT.PREPARATION_FAILED')
        status = ('cancelled' if code == 'PARSER.SOURCE.CANCELLED' else
                  'stale' if code in ('RPY.EXPORT.PREVIEW_STALE', 'RPY.EXPORT.TARGET_STALE') else 'blocked')
        candidate._stop(status, code)
    except BaseException:
        candidate.close()
        raise
    return candidate
