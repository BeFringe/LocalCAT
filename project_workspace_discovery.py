"""Project-owned bounded metadata discovery and explicit, revocable selection.

Only the observation port touches the filesystem. Selection does not verify
file bodies or current file snapshots: intake must bridge the retained original
root and selected metadata to its rooted sealed-read/verified-terminal facts
before forming a project candidate. No DTO carries filesystem authority.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import threading
from typing import Protocol
from uuid import uuid4

from parser_contracts import CodecDescriptor, EffectivePurpose, SelectionHints, SelectionRequest, SelectionFailure
from platform_fs_contracts import (
    DirectoryEntryMetadata, DirectoryObservationCancellation, DirectoryObservationLimits,
    EntrySnapshot, FileObjectIdentity, PlatformFileError, PlatformFileErrorCode,
    ReadOnlyDirectoryObservation, RetainedObservationDirectory,
)
from project_directory_contracts import (
    DirectoryDiscoveryRejection, DirectoryEntryKind, DirectoryEntryUnavailableReason,
    DirectoryRejectionCode, DirectorySelectionEntry, DirectorySelectionPreview,
    DirectorySelectionRequest, IssuedDirectorySelection,
)
from project_workspace_identity import (
    ProjectWorkspaceError, normalize_portable_ref_v1, validate_portable_ref_collection,
)


class _ParserSelectionSurface(Protocol):
    def select(self, request: SelectionRequest) -> CodecDescriptor | SelectionFailure: ...


class DirectoryDiscoveryError(ValueError):
    """Body-safe structured rejection; never contains native paths or messages."""

    def __init__(self, rejection: DirectoryDiscoveryRejection):
        self.rejection = rejection
        self.code = rejection.code.value
        super().__init__(self.code)


def _reject(code: DirectoryRejectionCode) -> None:
    raise DirectoryDiscoveryError(DirectoryDiscoveryRejection(code))


def _platform_rejection(error: PlatformFileError) -> DirectoryDiscoveryRejection:
    code = {
        PlatformFileErrorCode.OBSERVATION_CANCELLED.value: DirectoryRejectionCode.CANCELLED,
        PlatformFileErrorCode.OBSERVATION_LIMIT_EXCEEDED.value: DirectoryRejectionCode.LIMIT_EXCEEDED,
        PlatformFileErrorCode.IDENTITY_STALE.value: DirectoryRejectionCode.STALE,
    }.get(error.code, DirectoryRejectionCode.OBSERVATION_FAILED)
    return DirectoryDiscoveryRejection(code)


@dataclass(frozen=True, slots=True)
class SelectedDirectoryFile:
    """Owner-only original metadata; raw spelling is retained for safe rebinding."""

    entry_id: str
    source_ref: str
    raw_components: tuple[str, ...]
    snapshot: EntrySnapshot


@dataclass
class _Run:
    generation: int
    root_path: Path
    cancellation: DirectoryObservationCancellation = field(default_factory=DirectoryObservationCancellation)
    root: RetainedObservationDirectory | None = None
    preview: DirectorySelectionPreview | None = None
    files: dict[str, SelectedDirectoryFile] = field(default_factory=dict)
    selection: IssuedDirectorySelection | None = None


_BINDING_ISSUER = object()


class RetainedDirectorySelection:
    """Live owner-only view of one exact selection; the service owns all handles.

    Keep the service alive through intake/publication. Strict ``reprove`` lasts
    until verified intake takes over namespace proofs; that owner then combines
    ``check_current`` with its retained root, file snapshots and byte digests.
    Every property rejects revoked selections. ``files`` contains ORIGINAL
    observations, never current-file proofs or content-reading authority.
    This view cannot revive a generation or close a caller's shared service.
    """

    __slots__ = ("__service", "__selection")

    def __init__(self, service, selection, *, _issuer=None):
        if _issuer is not _BINDING_ISSUER:
            raise TypeError("retained selections must be issued by discovery")
        self.__service = service
        self.__selection = selection

    def __reduce__(self):
        raise TypeError("retained directory selections are live-only")

    def __copy__(self):
        raise TypeError("retained directory selections are live-only")

    def __deepcopy__(self, memo):
        raise TypeError("retained directory selections are live-only")

    def reprove(self) -> None:
        self.__service._selection_facts(self.__selection)

    def check_current(self) -> None:
        """Check revocation after verified intake takes over namespace proofs.

        Package publication may itself add entries under the original root.
        Its retained source owner proves the root and selected files; directory
        metadata is no longer required to remain unchanged at that stage.
        """
        self.__service._check_selection_current(self.__selection)

    @property
    def root_path(self) -> Path:
        return self.__service._selection_facts(self.__selection)[0]

    @property
    def root_identity(self) -> FileObjectIdentity:
        return self.__service._selection_facts(self.__selection)[1]

    @property
    def files(self) -> tuple[SelectedDirectoryFile, ...]:
        return self.__service._selection_facts(self.__selection)[2]


class ProjectDirectoryDiscoveryService:
    """One user selection lifecycle, with cancellable serialized observations.

    preview(root) starts a new generation (including refresh of the same root).
    cancel() revokes it promptly; an in-flight operation owns final cleanup as it
    unwinds. close() additionally prohibits future previews. No background work
    is started here. ``select`` preserves the exact caller-supplied review order;
    callers obtain initial order by filtering the already sorted preview entries.
    """

    def __init__(self, observation: ReadOnlyDirectoryObservation,
                 parser_surface: _ParserSelectionSurface, *,
                 limits: DirectoryObservationLimits = DirectoryObservationLimits()):
        if not isinstance(observation, ReadOnlyDirectoryObservation):
            raise TypeError("observation must support ReadOnlyDirectoryObservation")
        if not callable(getattr(parser_surface, "select", None)):
            raise TypeError("parser surface must provide neutral selection")
        if type(limits) is not DirectoryObservationLimits:
            raise TypeError("limits must be exact DirectoryObservationLimits")
        self._observation, self._parser, self._limits = observation, parser_surface, limits
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._generation = 0
        self._run: _Run | None = None
        self._retired: list[_Run] = []
        self._closed = False

    def _retire_locked(self) -> None:
        if self._run is not None:
            self._run.cancellation.cancel()
            self._run.selection = None
            self._retired.append(self._run)
            self._run = None
        self._generation += 1

    def _cleanup_retired(self) -> None:
        # Called only while the operation lock is owned: no scan can be using a
        # retired root. Native close failures remain terminal and are reported.
        with self._state_lock:
            retired, self._retired = self._retired, []
        failure = None
        for run in retired:
            if run.root is not None:
                try:
                    run.root.close()
                except BaseException as error:
                    failure = failure or error
                finally:
                    run.root = None
            run.files.clear()
        if failure is not None:
            raise failure

    def _finish_operation(self) -> None:
        # The empty-queue decision and ownership release share the same state
        # lock as retirement. A revoke either leaves work for this owner or can
        # acquire the released operation lock and drain it itself. Native close
        # stays outside the state lock so cancellation can always signal promptly.
        failure = None
        while True:
            with self._state_lock:
                if not self._retired:
                    self._operation_lock.release()
                    break
            try:
                self._cleanup_retired()
            except BaseException as error:
                failure = failure or error
        if failure is not None:
            raise failure

    @contextmanager
    def _operation(self) -> Iterator[None]:
        self._operation_lock.acquire()
        try:
            yield
        finally:
            self._finish_operation()

    def _current(self, run: _Run) -> None:
        with self._state_lock:
            if self._closed:
                _reject(DirectoryRejectionCode.CLOSED)
            if self._run is not run:
                _reject(DirectoryRejectionCode.CANCELLED)
            run.cancellation.check()

    def _revoke(self, *, close: bool) -> None:
        with self._state_lock:
            self._closed = self._closed or close
            self._retire_locked()
        if self._operation_lock.acquire(blocking=False):
            self._finish_operation()

    def cancel(self) -> None:
        self._revoke(close=False)

    def close(self) -> None:
        self._revoke(close=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _entry(self, metadata: DirectoryEntryMetadata, raw_components: tuple[str, ...],
               parent_id: str | None) -> DirectorySelectionEntry:
        kind = DirectoryEntryKind(metadata.kind.value)
        source_ref = None
        reason = None
        try:
            source_ref = normalize_portable_ref_v1("/".join(raw_components))
        except (ProjectWorkspaceError, UnicodeError):
            reason = DirectoryEntryUnavailableReason.INVALID_REFERENCE
        if metadata.unavailable_reason is not None:
            reason = {
                "link": DirectoryEntryUnavailableReason.LINK,
                "reparse": DirectoryEntryUnavailableReason.REPARSE,
                "not_regular": DirectoryEntryUnavailableReason.NOT_REGULAR,
                "unsafe_name": DirectoryEntryUnavailableReason.INVALID_REFERENCE,
            }[metadata.unavailable_reason.value]
        if reason is None:
            if kind is DirectoryEntryKind.DIRECTORY:
                reason = DirectoryEntryUnavailableReason.DIRECTORY
            else:
                suffix = PurePosixPath(source_ref).suffix
                descriptor = None
                if suffix:
                    try:
                        descriptor = self._parser.select(SelectionRequest(
                            EffectivePurpose.PROJECT_DOCUMENT,
                            hints=SelectionHints(extensions=(suffix,)),
                        ))
                    except ValueError:
                        # Unusable suffix hints are unavailable, never sniffed.
                        pass
                if (type(descriptor) is not CodecDescriptor
                        or descriptor.purpose is not EffectivePurpose.PROJECT_DOCUMENT
                        or not descriptor.capabilities.readable):
                    reason = DirectoryEntryUnavailableReason.UNSUPPORTED_FORMAT
        return DirectorySelectionEntry(
            uuid4().hex, parent_id, metadata.name, source_ref, kind,
            metadata.snapshot.byte_count if metadata.snapshot is not None else None,
            reason is None, reason,
        )

    def preview(self, root: Path) -> DirectorySelectionPreview:
        if not isinstance(root, Path):
            raise TypeError("root must be pathlib.Path")
        with self._state_lock:
            if self._closed:
                _reject(DirectoryRejectionCode.CLOSED)
            self._retire_locked()
            run = _Run(self._generation, root)
            self._run = run
        with self._operation():
            entries: list[tuple[DirectorySelectionEntry, tuple[str, ...]]] = []
            rejection = None
            published = False
            try:
                self._cleanup_retired()
                self._current(run)
                try:
                    run.root = self._observation.bind_observation_root(root, self._limits, run.cancellation)
                    self._current(run)

                    def walk(directory, components=(), parent_id=None):
                        self._current(run)
                        observed = self._observation.observe_children(directory, self._limits, run.cancellation)
                        children = []
                        for metadata in observed.entries:
                            self._current(run)
                            raw = components + (metadata.name,)
                            entry = self._entry(metadata, raw, parent_id)
                            entries.append((entry, raw))
                            if entry.selectable:
                                run.files[entry.entry_id] = SelectedDirectoryFile(
                                    entry.entry_id, entry.source_ref, raw, metadata.snapshot,
                                )
                            children.append((entry, raw, metadata))
                        for entry, raw, metadata in sorted(children, key=lambda item: (item[0].source_ref or "/".join(item[1]), item[1])):
                            self._current(run)
                            if entry.unavailable_reason is DirectoryEntryUnavailableReason.DIRECTORY:
                                with self._observation.retain_observed_directory(
                                        directory, observed, metadata, self._limits, run.cancellation) as child:
                                    walk(child, raw, entry.entry_id)
                        self._current(run)
                        directory.reprove()

                    walk(run.root)
                    self._current(run)
                except PlatformFileError as error:
                    rejection = _platform_rejection(error)
                    if rejection.code is DirectoryRejectionCode.CANCELLED:
                        raise DirectoryDiscoveryError(rejection) from None
                except (OSError, ValueError) as error:
                    if isinstance(error, DirectoryDiscoveryError):
                        raise
                    rejection = DirectoryDiscoveryRejection(DirectoryRejectionCode.OBSERVATION_FAILED)
                self._current(run)
                if rejection is not None:
                    if run.root is not None:
                        run.root.close()
                        run.root = None
                    run.files.clear()
                ordered = tuple(item[0] for item in sorted(entries, key=lambda item: (item[0].source_ref or "/".join(item[1]), item[1])))
                preview = DirectorySelectionPreview(uuid4().hex, run.generation, uuid4().hex,
                                                    ordered, rejection is None, rejection)
                with self._state_lock:
                    self._current(run)
                    run.preview = preview
                    published = True
                return preview
            finally:
                if not published:
                    with self._state_lock:
                        if self._run is run:
                            self._retire_locked()
                self._cleanup_retired()

    def _preview_run(self, preview_id: str, generation: int) -> _Run:
        if self._closed:
            _reject(DirectoryRejectionCode.CLOSED)
        run = self._run
        if (run is None or run.preview is None or run.generation != generation
                or run.preview.preview_id != preview_id):
            _reject(DirectoryRejectionCode.STALE)
        if not run.preview.complete:
            _reject(DirectoryRejectionCode.INCOMPLETE)
        return run

    def _reprove_root(self, run: _Run) -> FileObjectIdentity:
        try:
            self._current(run)
            identity = run.root.reprove()
            self._current(run)
            return identity
        except PlatformFileError as error:
            with self._state_lock:
                if self._run is run:
                    self._retire_locked()
            raise DirectoryDiscoveryError(_platform_rejection(error)) from None

    def select(self, request: DirectorySelectionRequest) -> IssuedDirectorySelection:
        if type(request) is not DirectorySelectionRequest:
            raise TypeError("request must be exact DirectorySelectionRequest")
        with self._operation():
            try:
                with self._state_lock:
                    run = self._preview_run(request.preview_id, request.generation)
                    if not request.entry_ids:
                        _reject(DirectoryRejectionCode.EMPTY_SELECTION)
                    if (len(set(request.entry_ids)) != len(request.entry_ids)
                            or any(entry_id not in run.files for entry_id in request.entry_ids)):
                        _reject(DirectoryRejectionCode.INVALID_SELECTION)
                    selected = tuple(run.files[entry_id] for entry_id in request.entry_ids)
                try:
                    validate_portable_ref_collection(tuple(item.source_ref for item in selected),
                                                     allow_exact_duplicates=False)
                except ProjectWorkspaceError:
                    _reject(DirectoryRejectionCode.ALIAS)
                identities = tuple(item.snapshot.identity for item in selected)
                physical_keys = tuple((item.platform, item.volume_id, item.file_id) for item in identities)
                if any(item.link_count != 1 for item in identities) or len(set(physical_keys)) != len(physical_keys):
                    _reject(DirectoryRejectionCode.ALIAS)
                self._reprove_root(run)
                with self._state_lock:
                    self._current(run)
                    selection = IssuedDirectorySelection(uuid4().hex, request.preview_id,
                        request.generation, run.preview.root_binding_ref, request.entry_ids)
                    run.selection = selection
                    return selection
            finally:
                self._cleanup_retired()

    def _check_selection_current(self, selection: IssuedDirectorySelection) -> _Run:
        with self._state_lock:
            if self._closed:
                _reject(DirectoryRejectionCode.CLOSED)
            if type(selection) is not IssuedDirectorySelection:
                _reject(DirectoryRejectionCode.STALE)
            run = self._preview_run(selection.preview_id, selection.generation)
            if run.selection is not selection:
                _reject(DirectoryRejectionCode.STALE)
            self._current(run)
            return run

    def _selection_facts(self, selection: IssuedDirectorySelection):
        with self._operation():
            try:
                run = self._check_selection_current(selection)
                identity = self._reprove_root(run)
                with self._state_lock:
                    self._current(run)
                    return run.root_path, identity, tuple(run.files[key] for key in selection.entry_ids)
            finally:
                self._cleanup_retired()

    def revalidate(self, selection: IssuedDirectorySelection) -> RetainedDirectorySelection:
        self._selection_facts(selection)
        return RetainedDirectorySelection(self, selection, _issuer=_BINDING_ISSUER)
