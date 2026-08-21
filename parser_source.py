"""Parser Foundation source, guarded-session, and atomic-byte boundaries.

This module is deliberately format-neutral.  It binds caller-selected files to a
safe root, copies one descriptor into a private sealed snapshot, lends read-only
offset-zero cursors, validates raw codec events, and performs atomic byte target
replacement.  It does not know LocalCAT schemas, Engine/Store objects, or any UI.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
from typing import Iterator

from parser_contracts import (
    CodecDescriptor,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    FOUNDATION_GUARDED_ISSUE_CODES,
    InputConsumptionPolicy,
    IssueCount,
    IssueSeverity,
    LimitProfile,
    ParseIssue,
    ParsedSegment,
    RawParseEvent,
    RawReaderCodec,
    ReadRequest,
    ResourceRecord,
    SeekableInputPreflightCodec,
    SourceReference,
    SourceSnapshotIdentity,
    TargetReference,
    TerminalSuccess,
    ValidationOutcome,
    ValidationReport,
    WriteReceipt,
    _issue_terminal_success,
    validate_metadata_container_increment,
)


_COPY_CHUNK_BYTES = 64 * 1024
_SNAPSHOT_SCHEMA_VERSION = 1
_WRITE_RECEIPT_SCHEMA_VERSION = 1


class ParserSourceError(ContractViolation):
    """Stable, body-safe failure at a rooted file or snapshot boundary."""


class ParserSessionError(ContractViolation):
    """Stable, body-safe failure that permanently denies session commit authority."""


class CancellationToken:
    """Thread-safe cancellation flag checked only at bounded Foundation seams."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ParserSourceError(
                "PARSER.SOURCE.CANCELLED",
                "the Parser operation was cancelled at a bounded checkpoint",
            )


def _check_cancellation(cancellation: CancellationToken | None) -> None:
    if cancellation is None:
        return
    if type(cancellation) is not CancellationToken:
        raise TypeError("cancellation must be an exact CancellationToken or None")
    cancellation.raise_if_cancelled()


def _rooted_handles_available() -> bool:
    return bool(
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "pread")
        and os.supports_dir_fd
        and os.open in os.supports_dir_fd
        and os.stat in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    )


def _require_rooted_handles() -> None:
    if not _rooted_handles_available():
        raise ParserSourceError(
            "PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE",
            "this platform cannot establish the required rooted file authority",
        )


def _open_flags(*, directory: bool) -> int:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    elif hasattr(os, "O_NONBLOCK"):
        # A FIFO/device must not block merely so Foundation can reject it after
        # fstat.  O_NONBLOCK has no semantic effect on a regular-file snapshot.
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _path_parts(path_text: str, *, require_absolute: bool) -> tuple[str, ...]:
    path = Path(path_text)
    if require_absolute and not path.is_absolute():
        raise ParserSourceError(
            "PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE",
            "safe root must be an absolute directory reference",
        )
    parts = path.parts
    if path.is_absolute():
        parts = parts[1:]
    if not parts and not path.is_absolute():
        raise ParserSourceError(
            "PARSER.SOURCE.OUTSIDE_ROOT",
            "the selected reference does not name a rooted file",
        )
    if any(part in {"", ".", ".."} for part in parts):
        raise ParserSourceError(
            "PARSER.SOURCE.OUTSIDE_ROOT",
            "the selected reference contains a component outside the safe root",
        )
    return tuple(parts)


def _relative_parts(safe_root: str, selected_path: str) -> tuple[str, ...]:
    root = Path(safe_root)
    selected = Path(selected_path)
    if not root.is_absolute():
        raise ParserSourceError(
            "PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE",
            "safe root must be an absolute directory reference",
        )
    if selected.is_absolute():
        try:
            relative = selected.relative_to(root)
        except ValueError as exc:
            raise ParserSourceError(
                "PARSER.SOURCE.OUTSIDE_ROOT",
                "the selected reference is outside the caller-provided safe root",
            ) from exc
    else:
        relative = selected
    parts = _path_parts(str(relative), require_absolute=False)
    if not parts:
        raise ParserSourceError(
            "PARSER.SOURCE.NOT_REGULAR",
            "the selected reference names the safe root instead of a regular file",
        )
    return parts


def _map_open_error(error: OSError) -> ParserSourceError:
    if error.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
        return ParserSourceError(
            "PARSER.SOURCE.NOT_REGULAR",
            "the rooted reference is a link, reparse point, or non-regular object",
        )
    return ParserSourceError(
        "PARSER.SOURCE.READ_FAILED",
        "the rooted regular file could not be opened for reading",
    )


def _open_absolute_root(safe_root: str) -> int:
    _require_rooted_handles()
    _path_parts(safe_root, require_absolute=True)
    try:
        # The caller owns safe-root selection.  Once its final component is opened
        # no-follow, the returned dirfd is the authority anchor; target components
        # below that retained handle are then opened one by one no-follow.  Walking
        # ancestors from '/' would incorrectly reject ordinary platform aliases
        # such as macOS /var -> /private/var before the caller's root is bound.
        current = os.open(safe_root, _open_flags(directory=True))
    except OSError as exc:
        raise _map_open_error(exc) from exc
    return current


@dataclass(slots=True)
class RootedRegularFile:
    """An already-bound descriptor; callers cannot reopen it by pathname."""

    _descriptor: int
    relative_path: str
    initial_status: os.stat_result
    _closed: bool = False

    @property
    def descriptor(self) -> int:
        if self._closed:
            raise ParserSourceError(
                "PARSER.SOURCE.READ_FAILED",
                "the rooted file descriptor is already closed",
            )
        return self._descriptor

    @property
    def is_regular_file(self) -> bool:
        return stat.S_ISREG(self.initial_status.st_mode)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._descriptor)

    def __enter__(self) -> RootedRegularFile:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


def open_rooted_regular_file(reference: SourceReference) -> RootedRegularFile:
    """Open one source through retained per-component no-follow dirfds."""

    if type(reference) is not SourceReference:
        raise TypeError("reference must be exact SourceReference")
    relative_parts = _relative_parts(reference.safe_root, reference.selected_path)
    current = _open_absolute_root(reference.safe_root)
    try:
        for component in relative_parts[:-1]:
            try:
                child = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=current,
                )
            except OSError as exc:
                raise _map_open_error(exc) from exc
            os.close(current)
            current = child
        try:
            descriptor = os.open(
                relative_parts[-1],
                _open_flags(directory=False),
                dir_fd=current,
            )
        except OSError as exc:
            raise _map_open_error(exc) from exc
    finally:
        os.close(current)
    try:
        status = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise ParserSourceError(
            "PARSER.SOURCE.READ_FAILED",
            "the rooted file identity could not be inspected safely",
        ) from exc
    if not stat.S_ISREG(status.st_mode):
        os.close(descriptor)
        raise ParserSourceError(
            "PARSER.SOURCE.NOT_REGULAR",
            "the rooted reference does not name a regular file",
        )
    return RootedRegularFile(
        _descriptor=descriptor,
        relative_path="/".join(relative_parts),
        initial_status=status,
    )


def _stable_status_key(status: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _relative_reference_digest(relative_path: str) -> str:
    return hashlib.sha256(relative_path.encode("utf-8", "strict")).hexdigest()


def _regular_file_identity(status: os.stat_result) -> str:
    return f"{status.st_dev}:{status.st_ino}"


@dataclass(frozen=True, slots=True)
class SnapshotExpectation:
    identity: SourceSnapshotIdentity
    limit_profile: LimitProfile

    def __post_init__(self) -> None:
        if type(self.identity) is not SourceSnapshotIdentity:
            raise TypeError("SnapshotExpectation.identity must be exact SourceSnapshotIdentity")
        if type(self.limit_profile) is not LimitProfile:
            raise TypeError("SnapshotExpectation.limit_profile must be exact LimitProfile")


class SealedSourceSnapshot:
    """Private anonymous bytes with lease-counted lifetime and immutable identity."""

    __slots__ = (
        "_temporary",
        "_limit_profile",
        "_source_name_hint",
        "_active_leases",
        "_active_seekable",
        "_release_requested",
        "_released",
        "_lock",
        "identity",
    )

    def __init__(
        self,
        temporary,
        *,
        identity: SourceSnapshotIdentity,
        limit_profile: LimitProfile,
        source_name_hint: str,
    ) -> None:
        self._temporary = temporary
        self.identity = identity
        self._limit_profile = limit_profile
        if type(source_name_hint) is not str or not source_name_hint:
            raise ValueError("source_name_hint must be a non-empty exact string")
        self._source_name_hint = source_name_hint
        self._active_leases = 0
        self._active_seekable = False
        self._release_requested = False
        self._released = False
        self._lock = threading.Lock()

    @property
    def expectation(self) -> SnapshotExpectation:
        return SnapshotExpectation(
            identity=self.identity,
            limit_profile=self._limit_profile,
        )

    @property
    def limit_profile(self) -> LimitProfile:
        return self._limit_profile

    @property
    def source_name_hint(self) -> str:
        return self._source_name_hint

    @property
    def release_requested(self) -> bool:
        return self._release_requested

    @property
    def released(self) -> bool:
        return self._released

    def lease(
        self,
        descriptor: CodecDescriptor,
        *,
        cancellation: CancellationToken | None = None,
    ):
        if type(descriptor) is not CodecDescriptor:
            raise TypeError("descriptor must be exact CodecDescriptor")
        if self.identity.byte_count > descriptor.limit_profile.max_input_bytes:
            raise ParserSourceError(
                "PARSER.LIMIT.INPUT",
                "sealed snapshot bytes exceed the active descriptor input limit",
            )
        if descriptor.limit_profile != self._limit_profile:
            raise ParserSourceError(
                "PARSER.SOURCE.STALE",
                "active descriptor limits differ from the sealed snapshot profile",
            )
        seekable = (
            descriptor.input_consumption_policy
            is InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
        )
        _check_cancellation(cancellation)
        with self._lock:
            if self._release_requested or self._released:
                raise ParserSourceError(
                    "PARSER.SOURCE.SNAPSHOT_RELEASED",
                    "the sealed snapshot no longer accepts cursor leases",
                )
            if seekable and self._active_seekable:
                raise ParserSourceError(
                    "PARSER.SOURCE.LEASE_CONFLICT",
                    "only one seekable snapshot lease may be active",
                )
            self._active_leases += 1
            if seekable:
                self._active_seekable = True
        lease_type = _SeekableSnapshotLease if seekable else _SequentialSnapshotLease
        return lease_type(self, cancellation=cancellation)

    def _lease_closed(self, *, seekable: bool) -> None:
        close_now = False
        with self._lock:
            if self._active_leases <= 0:
                return
            self._active_leases -= 1
            if seekable:
                self._active_seekable = False
            close_now = self._release_requested and self._active_leases == 0
        if close_now:
            self._close_temporary()

    def _close_temporary(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
            temporary = self._temporary
            self._temporary = None
        temporary.close()

    def close(self) -> None:
        close_now = False
        with self._lock:
            if self._release_requested or self._released:
                return
            self._release_requested = True
            close_now = self._active_leases == 0
        if close_now:
            self._close_temporary()

    @property
    def _descriptor(self) -> int:
        if self._released or self._temporary is None:
            raise ParserSourceError(
                "PARSER.SOURCE.SNAPSHOT_RELEASED",
                "the sealed snapshot bytes have been released",
            )
        return self._temporary.fileno()

    def __enter__(self) -> SealedSourceSnapshot:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()


class _SnapshotLeaseBase:
    __slots__ = ("_snapshot", "_offset", "_closed", "_cancellation", "_coverage")

    _seekable = False

    def __init__(
        self,
        snapshot: SealedSourceSnapshot,
        *,
        cancellation: CancellationToken | None,
    ) -> None:
        self._snapshot = snapshot
        self._offset = 0
        self._closed = False
        self._cancellation = cancellation
        self._coverage: list[tuple[int, int]] = []

    @property
    def source_identity(self) -> SourceSnapshotIdentity:
        return self._snapshot.identity

    @property
    def source_name_hint(self) -> str:
        return self._snapshot.source_name_hint

    @property
    def byte_count(self) -> int:
        return self._snapshot.identity.byte_count

    @property
    def consumption_proved(self) -> bool:
        if not self._seekable:
            return self._offset == self.byte_count
        return bool(
            self.byte_count == 0
            or (
                len(self._coverage) == 1
                and self._coverage[0] == (0, self.byte_count)
            )
        )

    @property
    def closed(self) -> bool:
        return self._closed

    def seekable(self) -> bool:
        return self._seekable

    def tell(self) -> int:
        self._require_open()
        return self._offset

    def _require_open(self) -> None:
        if self._closed:
            raise ParserSourceError(
                "PARSER.SOURCE.READ_FAILED",
                "the snapshot cursor lease is closed",
            )

    def read(self, size: int = -1) -> bytes:
        self._require_open()
        _check_cancellation(self._cancellation)
        if type(size) is not int:
            raise TypeError("read size must be an exact integer")
        if size < -1:
            raise ValueError("read size must be -1 or non-negative")
        remaining = self.byte_count - self._offset
        requested = remaining if size == -1 else min(size, remaining)
        chunks: list[bytes] = []
        unread = requested
        read_start = self._offset
        try:
            while unread:
                _check_cancellation(self._cancellation)
                chunk = os.pread(
                    self._snapshot._descriptor,
                    min(unread, _COPY_CHUNK_BYTES),
                    self._offset,
                )
                if not chunk:
                    raise ParserSourceError(
                        "PARSER.SOURCE.READ_FAILED",
                        "the sealed snapshot ended before its bound byte count",
                    )
                chunks.append(chunk)
                self._offset += len(chunk)
                unread -= len(chunk)
            _check_cancellation(self._cancellation)
        except ParserSourceError:
            raise
        except OSError as exc:
            raise ParserSourceError(
                "PARSER.SOURCE.READ_FAILED",
                "the sealed snapshot cursor could not read its bound bytes",
            ) from exc
        if self._offset > read_start:
            self._record_coverage(read_start, self._offset)
        return b"".join(chunks)

    def _record_coverage(self, start: int, end: int) -> None:
        if start >= end:
            return
        intervals = sorted((*self._coverage, (start, end)))
        merged: list[tuple[int, int]] = []
        for interval_start, interval_end in intervals:
            if not merged or interval_start > merged[-1][1]:
                merged.append((interval_start, interval_end))
                continue
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, interval_end))
        self._coverage = merged

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._snapshot._lease_closed(seekable=self._seekable)

    def __enter__(self):
        self._require_open()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class _SequentialSnapshotLease(_SnapshotLeaseBase):
    pass


class _SeekableSnapshotLease(_SnapshotLeaseBase):
    _seekable = True

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._require_open()
        _check_cancellation(self._cancellation)
        if type(offset) is not int or type(whence) is not int:
            raise TypeError("seek offset and whence must be exact integers")
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._offset + offset
        elif whence == os.SEEK_END:
            position = self.byte_count + offset
        else:
            raise ValueError("unsupported seek whence")
        if position < 0 or position > self.byte_count:
            raise ValueError("seek position is outside the sealed snapshot")
        self._offset = position
        return position

def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise OSError(errno.EIO, "short write")
        written += count


def create_sealed_snapshot(
    reference: SourceReference,
    *,
    limit_profile: LimitProfile,
    cancellation: CancellationToken | None = None,
) -> SealedSourceSnapshot:
    """Copy one rooted descriptor once while hashing and proving fstat stability."""

    if type(reference) is not SourceReference:
        raise TypeError("reference must be exact SourceReference")
    if type(limit_profile) is not LimitProfile:
        raise TypeError("limit_profile must be exact LimitProfile")
    _check_cancellation(cancellation)
    temporary = None
    try:
        with open_rooted_regular_file(reference) as opened:
            before = os.fstat(opened.descriptor)
            if before.st_size > limit_profile.max_input_bytes:
                raise ParserSourceError(
                    "PARSER.LIMIT.INPUT",
                    "source bytes exceed the active input limit",
                )
            temporary = tempfile.TemporaryFile(mode="w+b", prefix="parser-snapshot-")
            digest = hashlib.sha256()
            copied = 0
            while True:
                _check_cancellation(cancellation)
                chunk = os.read(opened.descriptor, _COPY_CHUNK_BYTES)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > limit_profile.max_input_bytes:
                    raise ParserSourceError(
                        "PARSER.LIMIT.INPUT",
                        "source bytes exceed the active input limit",
                    )
                digest.update(chunk)
                _write_all(temporary.fileno(), chunk)
            _check_cancellation(cancellation)
            after = os.fstat(opened.descriptor)
            if _stable_status_key(before) != _stable_status_key(after) or copied != before.st_size:
                raise ParserSourceError(
                    "PARSER.SOURCE.STALE",
                    "source identity changed while the sealed snapshot was copied",
                )
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary.seek(0)
            identity = SourceSnapshotIdentity(
                relative_reference_sha256=_relative_reference_digest(opened.relative_path),
                regular_file_identity=_regular_file_identity(before),
                original_size=before.st_size,
                original_mtime_ns=before.st_mtime_ns,
                content_sha256=digest.hexdigest(),
                byte_count=copied,
                schema_version=_SNAPSHOT_SCHEMA_VERSION,
            )
            source_name_hint = opened.relative_path.rsplit("/", 1)[-1]
        snapshot = SealedSourceSnapshot(
            temporary,
            identity=identity,
            limit_profile=limit_profile,
            source_name_hint=source_name_hint,
        )
        temporary = None
        return snapshot
    except ParserSourceError:
        raise
    except OSError as exc:
        raise ParserSourceError(
            "PARSER.SOURCE.READ_FAILED",
            "the rooted source could not be copied into a sealed snapshot",
        ) from exc
    finally:
        if temporary is not None:
            temporary.close()


def reopen_sealed_snapshot(
    reference: SourceReference,
    *,
    limit_profile: LimitProfile,
    expected: SnapshotExpectation,
    cancellation: CancellationToken | None = None,
) -> SealedSourceSnapshot:
    """Rebuild released bytes and reject any identity/profile drift before parsing."""

    if type(expected) is not SnapshotExpectation:
        raise TypeError("expected must be exact SnapshotExpectation")
    if type(limit_profile) is not LimitProfile:
        raise TypeError("limit_profile must be exact LimitProfile")
    if expected.limit_profile != limit_profile:
        raise ParserSourceError(
            "PARSER.SOURCE.STALE",
            "the active codec limit profile differs from the validated snapshot",
        )
    snapshot = create_sealed_snapshot(
        reference,
        limit_profile=limit_profile,
        cancellation=cancellation,
    )
    if snapshot.identity != expected.identity:
        snapshot.close()
        raise ParserSourceError(
            "PARSER.SOURCE.STALE",
            "the current source identity differs from the validated snapshot",
        )
    return snapshot


class _SessionState(Enum):
    NEW = "new"
    RUNNING = "running"
    EOF_VERIFIED = "eof_verified"
    FAILED = "failed"
    ABORTED = "aborted"
    TERMINAL_ISSUED = "terminal_issued"


class _SessionView(Enum):
    ITERATOR = "iterator"
    VALIDATION = "validation"
    MATERIALIZED = "materialized"


class GuardedParseSession:
    """Foundation-owned verifier around the codec's sole raw grammar stream."""

    def __init__(
        self,
        codec: RawReaderCodec,
        source: SealedSourceSnapshot,
        request: ReadRequest,
        *,
        cancellation: CancellationToken | None = None,
        _view: _SessionView = _SessionView.ITERATOR,
    ) -> None:
        descriptor = getattr(codec, "descriptor", None)
        if type(descriptor) is not CodecDescriptor:
            raise TypeError("codec.descriptor must be exact CodecDescriptor")
        if type(source) is not SealedSourceSnapshot:
            raise TypeError("source must be exact SealedSourceSnapshot")
        if type(request) is not ReadRequest:
            raise TypeError("request must be exact ReadRequest")
        if type(_view) is not _SessionView:
            raise TypeError("_view must be exact _SessionView")
        if request.purpose is not descriptor.purpose or request.format_id != descriptor.format_id:
            raise ParserSessionError(
                "PARSER.SELECTION.UNSUPPORTED",
                "read request does not match the selected codec descriptor",
            )
        missing_codes = tuple(
            sorted(
                set(FOUNDATION_GUARDED_ISSUE_CODES)
                - set(descriptor.limit_profile.declared_issue_codes)
            )
        )
        if missing_codes:
            raise ParserSessionError(
                "PARSER.CAPABILITY.DESCRIPTOR_INVALID",
                "codec profile omits mandatory Foundation issue codes",
            )
        capabilities = descriptor.capabilities
        unsupported_code = None
        unsupported_summary = None
        if _view is _SessionView.ITERATOR and not capabilities.iterator_view:
            unsupported_code = "PARSER.CAPABILITY.ITERATOR_VIEW_UNSUPPORTED"
            unsupported_summary = "selected codec does not support iterator view"
        elif _view is _SessionView.VALIDATION and not capabilities.validatable:
            unsupported_code = "PARSER.CAPABILITY.VALIDATION_UNSUPPORTED"
            unsupported_summary = "selected codec does not support validation"
        elif _view is _SessionView.MATERIALIZED and not capabilities.materialized_view:
            unsupported_code = "PARSER.CAPABILITY.MATERIALIZED_VIEW_UNSUPPORTED"
            unsupported_summary = "selected codec does not support materialized view"
        if unsupported_code is not None:
            raise ParserSessionError(unsupported_code, unsupported_summary)
        if (
            descriptor.input_consumption_policy
            is InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET
            and not isinstance(codec, SeekableInputPreflightCodec)
        ):
            raise ParserSessionError(
                "PARSER.CAPABILITY.PREFLIGHT_UNSUPPORTED",
                "seekable container policy requires the declared preflight behavior",
            )
        _check_cancellation(cancellation)
        try:
            lease = source.lease(descriptor, cancellation=cancellation)
        except ParserSourceError as exc:
            raise ParserSessionError(exc.code, exc.safe_summary) from exc
        self.codec = codec
        self.descriptor = descriptor
        self.snapshot = source
        self.source = lease
        self.request = request
        self._view = _view
        self._cancellation = cancellation
        self._state = _SessionState.NEW
        self._header: DocumentHeader | None = None
        self._record_count = 0
        self._local_ids: set[str] = set()
        self._issue_counter: Counter[tuple[str, IssueSeverity]] = Counter()
        self._retained_issues: list[ParseIssue] = []
        self._issues_truncated = False
        self._metadata_total = 0
        self._primary_fatal: ParseIssue | None = None
        self._raw_iterator = None
        self._preflight_verified = False

    @property
    def header(self) -> DocumentHeader | None:
        return self._header

    @property
    def provisional_record_count(self) -> int:
        return self._record_count

    @property
    def retained_issues(self) -> tuple[ParseIssue, ...]:
        return tuple(self._retained_issues)

    @property
    def issues_truncated(self) -> bool:
        return self._issues_truncated

    @property
    def primary_fatal(self) -> ParseIssue | None:
        return self._primary_fatal

    @property
    def issue_counts(self) -> tuple[IssueCount, ...]:
        return tuple(
            IssueCount(code=code, severity=severity, count=count)
            for (code, severity), count in sorted(
                self._issue_counter.items(),
                key=lambda item: (item[0][0], item[0][1].value),
            )
        )

    def __iter__(self) -> Iterator[RawParseEvent]:
        return self._events()

    def _events(self) -> Iterator[RawParseEvent]:
        if self._state is not _SessionState.NEW:
            if self._state is _SessionState.FAILED:
                raise self._error_from_failure()
            if self._state is _SessionState.ABORTED:
                raise ParserSessionError(
                    "PARSER.SESSION.ABORTED",
                    "guarded parse session was closed before consumption",
                )
            raise ParserSessionError(
                "PARSER.SESSION.ALREADY_CONSUMED",
                "a guarded parse session can consume its raw grammar only once",
            )
        self._state = _SessionState.RUNNING
        completed = False
        try:
            self._run_input_preflight()
            try:
                self._raw_iterator = iter(
                    self.codec.iter_raw(self.source, self.request)
                )
            except BaseException as exc:
                self._fail(
                    "PARSER.SYNTAX.MALFORMED",
                    "raw codec could not establish its declared grammar stream",
                )
                raise self._error_from_failure() from exc
            while True:
                self._require_running()
                self._check_cancelled()
                try:
                    event = next(self._raw_iterator)
                except StopIteration:
                    self._require_running()
                    try:
                        extra = next(self._raw_iterator)
                    except StopIteration:
                        self._require_running()
                        self._verify_eof()
                        completed = True
                        return
                    except BaseException as exc:
                        self._fail(
                            "PARSER.SYNTAX.MALFORMED",
                            "raw codec failed after reporting the end of its grammar",
                        )
                        raise self._error_from_failure() from exc
                    else:
                        del extra
                        self._fail(
                            "PARSER.SYNTAX.INVALID_EVENT",
                            "raw codec emitted an event after an observed EOF",
                        )
                        raise self._error_from_failure()
                except ParserSourceError as exc:
                    self._fail(exc.code, exc.safe_summary)
                    raise self._error_from_failure() from exc
                except BaseException as exc:
                    self._fail(
                        "PARSER.SYNTAX.MALFORMED",
                        "raw codec terminated with an untrusted grammar failure",
                    )
                    raise self._error_from_failure() from exc
                self._require_running()
                self._validate_event(event)
                self._require_running()
                yield event
        finally:
            if not completed and self._state is _SessionState.RUNNING:
                self._state = _SessionState.ABORTED
            if not completed and self._raw_iterator is not None:
                close = getattr(self._raw_iterator, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    def _run_input_preflight(self) -> None:
        policy = self.descriptor.input_consumption_policy
        if policy is not InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET:
            return
        if type(self.source) is not _SeekableSnapshotLease:
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "seekable input preflight requires a Foundation seekable lease",
            )
            raise self._error_from_failure()
        # __init__ already established the structural behavior contract before
        # issuing the lease.  Keep this guard local so a later mutation cannot
        # turn the preflight call into an arbitrary attribute failure.
        if not isinstance(self.codec, SeekableInputPreflightCodec):
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "seekable input preflight behavior is no longer available",
            )
            raise self._error_from_failure()
        self._check_cancelled()
        try:
            result = self.codec.preflight_input(self.source, self.request)
        except ParserSourceError as exc:
            self._fail(exc.code, exc.safe_summary)
            raise self._error_from_failure() from exc
        except BaseException as exc:
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "seekable input preflight failed before raw grammar",
            )
            raise self._error_from_failure() from exc
        self._require_running()
        if result is not None:
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "seekable input preflight returned an unsupported authority token",
            )
            raise self._error_from_failure()
        try:
            at_origin = self.source.tell() == 0
        except ParserSourceError as exc:
            self._fail(exc.code, exc.safe_summary)
            raise self._error_from_failure() from exc
        if not self.source.consumption_proved or not at_origin:
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "seekable input preflight did not prove complete sealed-input coverage at origin",
            )
            raise self._error_from_failure()
        self._preflight_verified = True

    def _require_running(self) -> None:
        if self._state is _SessionState.RUNNING:
            return
        if self._state is _SessionState.ABORTED:
            raise ParserSessionError(
                "PARSER.SESSION.ABORTED",
                "guarded parse session was closed before verified EOF",
            )
        raise self._error_from_failure()

    def _check_cancelled(self) -> None:
        try:
            _check_cancellation(self._cancellation)
        except ParserSourceError as exc:
            self._fail(exc.code, exc.safe_summary)
            raise self._error_from_failure() from exc

    def _validate_event(self, event: object) -> None:
        profile = self.descriptor.limit_profile
        purpose = self.request.purpose
        if type(event) is DocumentHeader:
            if purpose is not EffectivePurpose.PROJECT_DOCUMENT or self._header is not None:
                self._fail(
                    "PARSER.SYNTAX.INVALID_HEADER",
                    "raw document header cardinality does not match the declared purpose",
                )
                raise self._error_from_failure()
            if self._record_count:
                self._fail(
                    "PARSER.SYNTAX.INVALID_HEADER",
                    "the project document header must precede project records",
                )
                raise self._error_from_failure()
            self._check_fields(event.name, event.source_locale, event.target_locale)
            self._accept_metadata(event.metadata)
            self._header = event
            return
        if type(event) is ParsedSegment:
            if purpose is EffectivePurpose.PROJECT_DOCUMENT and self._header is None:
                self._fail(
                    "PARSER.SYNTAX.INVALID_HEADER",
                    "project segment appeared before its single document header",
                )
                raise self._error_from_failure()
            if purpose is not EffectivePurpose.PROJECT_DOCUMENT:
                self._fail(
                    "PARSER.SYNTAX.INVALID_EVENT",
                    "project segment does not match the declared raw stream purpose",
                )
                raise self._error_from_failure()
            self._accept_record(event)
            return
        if type(event) is ResourceRecord:
            if purpose is EffectivePurpose.PROJECT_DOCUMENT:
                self._fail(
                    "PARSER.SYNTAX.INVALID_EVENT",
                    "resource record does not match the declared raw stream purpose",
                )
                raise self._error_from_failure()
            self._accept_record(event)
            return
        if type(event) is ParseIssue:
            if event.code not in profile.declared_issue_codes:
                self._fail(
                    "PARSER.PLUGIN.ISSUE_UNDECLARED",
                    "raw codec emitted an issue outside its declared finite allowlist",
                )
                raise self._error_from_failure()
            self._record_issue(event)
            if event.severity is IssueSeverity.FATAL:
                if self._primary_fatal is None:
                    self._primary_fatal = event
                self._state = _SessionState.FAILED
                raise ParserSessionError(event.code, event.safe_summary)
            return
        self._fail(
            "PARSER.SYNTAX.INVALID_EVENT",
            "raw codec emitted an event outside the closed Foundation event set",
        )
        raise self._error_from_failure()

    def _accept_record(self, event: ParsedSegment | ResourceRecord) -> None:
        profile = self.descriptor.limit_profile
        if event.local_id in self._local_ids:
            self._fail(
                "PARSER.SYNTAX.DUPLICATE_LOCAL_ID",
                "raw codec emitted a duplicate local record identity",
            )
            raise self._error_from_failure()
        if self._record_count >= profile.max_records:
            self._fail(
                "PARSER.LIMIT.RECORD",
                "raw record count exceeds the active limit profile",
            )
            raise self._error_from_failure()
        fields: tuple[str | None, ...]
        if type(event) is ParsedSegment:
            fields = (event.local_id, event.source, event.target, event.speaker.value)
        else:
            fields = (
                event.local_id,
                event.source,
                event.target,
                event.speaker.value,
            )
        self._check_fields(*fields)
        self._accept_metadata(event.format_metadata)
        self._local_ids.add(event.local_id)
        self._record_count += 1

    def _accept_metadata(self, container) -> None:
        try:
            self._metadata_total = validate_metadata_container_increment(
                container,
                limit_profile=self.descriptor.limit_profile,
                prior_total_decoded_chars=self._metadata_total,
            )
        except ContractViolation as exc:
            self._fail(exc.code, exc.safe_summary)
            raise self._error_from_failure() from exc

    def _check_fields(self, *values: str | None) -> None:
        maximum = self.descriptor.limit_profile.max_decoded_field_chars
        if any(value is not None and len(value) > maximum for value in values):
            self._fail(
                "PARSER.LIMIT.FIELD",
                "decoded field characters exceed the active limit profile",
            )
            raise self._error_from_failure()

    def _record_issue(self, issue: ParseIssue) -> None:
        key = (issue.code, issue.severity)
        self._issue_counter[key] += 1
        if len(self._retained_issues) < self.descriptor.limit_profile.max_retained_issues:
            self._retained_issues.append(issue)
            return
        self._issues_truncated = True
        if issue.severity is IssueSeverity.FATAL:
            for index in range(len(self._retained_issues) - 1, -1, -1):
                if self._retained_issues[index].severity is IssueSeverity.WARNING:
                    self._retained_issues[index] = issue
                    return

    def _fail(self, code: str, safe_summary: str) -> None:
        if self._state in {_SessionState.ABORTED, _SessionState.TERMINAL_ISSUED}:
            return
        issue = ParseIssue(
            code=code,
            severity=IssueSeverity.FATAL,
            safe_summary=safe_summary,
        )
        if self._primary_fatal is None:
            self._primary_fatal = issue
        self._record_issue(issue)
        self._state = _SessionState.FAILED

    def _error_from_failure(self) -> ParserSessionError:
        if self._primary_fatal is not None:
            return ParserSessionError(
                self._primary_fatal.code,
                self._primary_fatal.safe_summary,
            )
        if self._state is _SessionState.ABORTED:
            return ParserSessionError(
                "PARSER.SESSION.ABORTED",
                "guarded parse session was closed before verified EOF",
            )
        return ParserSessionError(
            "PARSER.SYNTAX.MALFORMED",
            "guarded raw stream failed without a verified terminal",
        )

    def _verify_eof(self) -> None:
        self._require_running()
        if self._primary_fatal is not None or any(
            severity is IssueSeverity.FATAL
            for _code, severity in self._issue_counter
        ):
            raise self._error_from_failure()
        if self.request.purpose is EffectivePurpose.PROJECT_DOCUMENT and self._header is None:
            self._fail(
                "PARSER.SYNTAX.INVALID_HEADER",
                "project document raw stream ended without exactly one header",
            )
            raise self._error_from_failure()
        policy = self.descriptor.input_consumption_policy
        if policy is InputConsumptionPolicy.XLSX_PREFLIGHT_ACTIVE_SHEET:
            if (
                type(self.source) is not _SeekableSnapshotLease
                or not self._preflight_verified
            ):
                self._fail(
                    "PARSER.SOURCE.READ_FAILED",
                    "seekable raw grammar ended without verified input preflight",
                )
                raise self._error_from_failure()
        elif type(self.source) is not _SequentialSnapshotLease:
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "sequential consumption proof requires a Foundation sequential lease",
            )
            raise self._error_from_failure()
        if not self.source.consumption_proved:
            self._fail(
                "PARSER.SOURCE.READ_FAILED",
                "raw grammar ended before proving complete sealed-input consumption",
            )
            raise self._error_from_failure()
        self._require_running()
        self._state = _SessionState.EOF_VERIFIED

    def verified_terminal(self) -> TerminalSuccess:
        if self._state is _SessionState.TERMINAL_ISSUED:
            raise ParserSessionError(
                "PARSER.SESSION.TERMINAL_ALREADY_ISSUED",
                "the guarded session terminal was already issued",
            )
        if self._state is not _SessionState.EOF_VERIFIED:
            code = (
                "PARSER.SESSION.ABORTED"
                if self._state in {_SessionState.ABORTED, _SessionState.NEW, _SessionState.RUNNING}
                else "PARSER.SESSION.UNVERIFIED"
            )
            raise ParserSessionError(
                code,
                "guarded session did not reach a complete verified raw EOF",
            )
        if self.source.closed or self._primary_fatal is not None:
            raise ParserSessionError(
                "PARSER.SESSION.UNVERIFIED",
                "verified terminal requires a live lease and zero fatal issues",
            )
        warning_counts = tuple(
            count
            for count in self.issue_counts
            if count.severity is IssueSeverity.WARNING
        )
        terminal = _issue_terminal_success(
            source=self.source.source_identity,
            codec_identity=self.descriptor.identity,
            limit_profile=self.descriptor.limit_profile,
            record_count=self._record_count,
            warning_counts=warning_counts,
            issues_truncated=self._issues_truncated,
        )
        self._state = _SessionState.TERMINAL_ISSUED
        return terminal

    def abort(self, code: str, safe_summary: str) -> None:
        if self._state is _SessionState.TERMINAL_ISSUED:
            raise ParserSessionError(
                "PARSER.SESSION.UNVERIFIED",
                "an issued terminal cannot be retroactively aborted",
            )
        if self._state in {_SessionState.FAILED, _SessionState.ABORTED}:
            return
        if code not in self.descriptor.limit_profile.declared_issue_codes:
            code = "PARSER.PLUGIN.ISSUE_UNDECLARED"
            safe_summary = "abort reason is outside the declared finite issue allowlist"
        self._fail(code, safe_summary)
        self._close_raw_iterator()
        self.source.close()

    def close(self) -> None:
        if self._state in {
            _SessionState.NEW,
            _SessionState.RUNNING,
            _SessionState.EOF_VERIFIED,
        }:
            self._state = _SessionState.ABORTED
        self._close_raw_iterator()
        self.source.close()

    def _close_raw_iterator(self) -> None:
        if self._raw_iterator is None:
            return
        close = getattr(self._raw_iterator, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def failed_report(self, *, cancelled: bool = False) -> ValidationReport:
        if self._primary_fatal is None:
            raise ParserSessionError(
                "PARSER.SESSION.ABORTED",
                "a session without a primary fatal cannot produce a failed report",
            )
        outcome = ValidationOutcome.CANCELLED if cancelled else ValidationOutcome.FAILED
        return ValidationReport(
            outcome=outcome,
            source=self.source.source_identity,
            format_id=self.descriptor.format_id,
            codec_identity=self.descriptor.identity,
            observed_capabilities=self.descriptor.capabilities,
            limit_profile=self.descriptor.limit_profile,
            provisional_record_count=self._record_count,
            issue_counts=self.issue_counts,
            issues=self.retained_issues,
            issues_truncated=self._issues_truncated,
            terminal=None,
        )


@dataclass(frozen=True, slots=True)
class MaterializedParseResult:
    header: DocumentHeader | None
    records: tuple[ParsedSegment | ResourceRecord, ...]
    issues: tuple[ParseIssue, ...]
    terminal: TerminalSuccess


def validate(
    codec: RawReaderCodec,
    snapshot: SealedSourceSnapshot,
    request: ReadRequest,
    *,
    cancellation: CancellationToken | None = None,
) -> ValidationReport:
    """Consume the codec's only raw grammar and report its guarded outcome."""

    try:
        session = GuardedParseSession(
            codec,
            snapshot,
            request,
            cancellation=cancellation,
            _view=_SessionView.VALIDATION,
        )
    except ParserSourceError as exc:
        if not code_is_cancelled(exc.code):
            raise
        descriptor = getattr(codec, "descriptor", None)
        if type(descriptor) is not CodecDescriptor:
            raise TypeError("codec.descriptor must be exact CodecDescriptor") from exc
        issue = ParseIssue(
            code=exc.code,
            severity=IssueSeverity.FATAL,
            safe_summary=exc.safe_summary,
        )
        return ValidationReport(
            outcome=ValidationOutcome.CANCELLED,
            source=snapshot.identity,
            format_id=descriptor.format_id,
            codec_identity=descriptor.identity,
            observed_capabilities=descriptor.capabilities,
            limit_profile=descriptor.limit_profile,
            provisional_record_count=0,
            issue_counts=(IssueCount(exc.code, IssueSeverity.FATAL, 1),),
            issues=(issue,),
            issues_truncated=False,
            terminal=None,
        )
    try:
        for _event in session:
            pass
        terminal = session.verified_terminal()
    except ParserSessionError as exc:
        report = session.failed_report(cancelled=code_is_cancelled(exc.code))
        session.close()
        return report
    report = ValidationReport(
            outcome=ValidationOutcome.SUCCESS,
            source=session.source.source_identity,
            format_id=session.descriptor.format_id,
            codec_identity=session.descriptor.identity,
            observed_capabilities=session.descriptor.capabilities,
            limit_profile=session.descriptor.limit_profile,
            provisional_record_count=session.provisional_record_count,
            issue_counts=session.issue_counts,
            issues=session.retained_issues,
            issues_truncated=session.issues_truncated,
            terminal=terminal,
        )
    session.close()
    return report


def code_is_cancelled(code: str) -> bool:
    return code == "PARSER.SOURCE.CANCELLED"


def materialize(
    codec: RawReaderCodec,
    snapshot: SealedSourceSnapshot,
    request: ReadRequest,
    *,
    cancellation: CancellationToken | None = None,
) -> MaterializedParseResult:
    """Materialize the same guarded grammar within the descriptor's explicit cap."""

    session = GuardedParseSession(
        codec,
        snapshot,
        request,
        cancellation=cancellation,
        _view=_SessionView.MATERIALIZED,
    )
    records: list[ParsedSegment | ResourceRecord] = []
    iterator = iter(session)
    try:
        for event in iterator:
            if type(event) in {ParsedSegment, ResourceRecord}:
                if len(records) >= session.descriptor.limit_profile.max_materialized_records:
                    session.abort(
                        "PARSER.LIMIT.MATERIALIZATION",
                        "materialized records exceed the active limit profile",
                    )
                    raise ParserSessionError(
                        "PARSER.LIMIT.MATERIALIZATION",
                        "materialized records exceed the active limit profile",
                    )
                records.append(event)
        terminal = session.verified_terminal()
        return MaterializedParseResult(
            header=session.header,
            records=tuple(records),
            issues=session.retained_issues,
            terminal=terminal,
        )
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
        session.close()


def _open_rooted_target_parent(reference: TargetReference) -> tuple[int, str, str]:
    if type(reference) is not TargetReference:
        raise TypeError("reference must be exact TargetReference")
    relative_parts = _relative_parts(reference.safe_root, reference.selected_path)
    current = _open_absolute_root(reference.safe_root)
    try:
        for component in relative_parts[:-1]:
            try:
                child = os.open(
                    component,
                    _open_flags(directory=True),
                    dir_fd=current,
                )
            except OSError as exc:
                raise _map_open_error(exc) from exc
            os.close(current)
            current = child
        target_name = relative_parts[-1]
        try:
            target_status = os.stat(
                target_name,
                dir_fd=current,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            target_status = None
        except OSError as exc:
            raise _map_open_error(exc) from exc
        if target_status is not None and not stat.S_ISREG(target_status.st_mode):
            raise ParserSourceError(
                "PARSER.SOURCE.NOT_REGULAR",
                "the rooted target is a link or non-regular object",
            )
        return current, target_name, "/".join(relative_parts)
    except BaseException:
        os.close(current)
        raise


def _validate_temp_payload(
    descriptor: int,
    *,
    expected_digest: str,
    expected_byte_count: int,
) -> os.stat_result:
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_size != expected_byte_count:
        raise ValueError("temporary target identity or byte count is invalid")
    digest = hashlib.sha256()
    offset = 0
    while offset < expected_byte_count:
        chunk = os.pread(
            descriptor,
            min(_COPY_CHUNK_BYTES, expected_byte_count - offset),
            offset,
        )
        if not chunk:
            raise ValueError("temporary target ended before its expected byte count")
        digest.update(chunk)
        offset += len(chunk)
    if digest.hexdigest() != expected_digest:
        raise ValueError("temporary target digest differs from serialized bytes")
    return status


def _prove_replaced_target(
    parent_descriptor: int,
    target_name: str,
    *,
    expected_digest: str,
    expected_byte_count: int,
) -> os.stat_result:
    """Reopen the actual replaced target through retained authority and prove bytes."""

    descriptor = None
    try:
        descriptor = os.open(
            target_name,
            _open_flags(directory=False),
            dir_fd=parent_descriptor,
        )
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_size != expected_byte_count:
            raise ParserSourceError(
                "PARSER.SOURCE.WRITE_PROOF_FAILED",
                "actual replaced target identity or byte count differs from receipt input",
            )
        digest = hashlib.sha256()
        offset = 0
        while offset < expected_byte_count:
            chunk = os.pread(
                descriptor,
                min(_COPY_CHUNK_BYTES, expected_byte_count - offset),
                offset,
            )
            if not chunk:
                raise ParserSourceError(
                    "PARSER.SOURCE.WRITE_PROOF_FAILED",
                    "actual replaced target ended before its receipt byte count",
                )
            digest.update(chunk)
            offset += len(chunk)
        if digest.hexdigest() != expected_digest:
            raise ParserSourceError(
                "PARSER.SOURCE.WRITE_PROOF_FAILED",
                "actual replaced target digest differs from serialized bytes",
            )
        return status
    except ParserSourceError:
        raise
    except OSError as exc:
        raise ParserSourceError(
            "PARSER.SOURCE.WRITE_PROOF_FAILED",
            "actual replaced target could not be proven through retained authority",
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def atomic_write_bytes(reference: TargetReference, payload: bytes) -> WriteReceipt:
    """Replace one rooted regular-file target only after durable temp validation."""

    if type(reference) is not TargetReference:
        raise TypeError("reference must be exact TargetReference")
    if type(payload) is not bytes:
        raise TypeError("payload must be exact bytes")
    parent_descriptor = None
    temporary_descriptor = None
    temporary_name = None
    replaced = False
    try:
        parent_descriptor, target_name, relative_path = _open_rooted_target_parent(reference)
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        for _attempt in range(32):
            candidate = f".parser-{secrets.token_hex(16)}.tmp"
            try:
                temporary_descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=parent_descriptor,
                )
                temporary_name = candidate
                break
            except FileExistsError:
                continue
        if temporary_descriptor is None or temporary_name is None:
            raise ParserSourceError(
                "PARSER.SOURCE.WRITE_FAILED",
                "an exclusive rooted temporary target could not be created",
            )
        digest = hashlib.sha256(payload).hexdigest()
        _write_all(temporary_descriptor, payload)
        os.fsync(temporary_descriptor)
        try:
            _validate_temp_payload(
                temporary_descriptor,
                expected_digest=digest,
                expected_byte_count=len(payload),
            )
        except ValueError as exc:
            raise ParserSourceError(
                "PARSER.SOURCE.WRITE_VALIDATION_FAILED",
                "temporary target validation failed before atomic replacement",
            ) from exc
        os.close(temporary_descriptor)
        temporary_descriptor = None
        os.replace(
            temporary_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        replaced = True
        actual_status = _prove_replaced_target(
            parent_descriptor,
            target_name,
            expected_digest=digest,
            expected_byte_count=len(payload),
        )
        return WriteReceipt(
            target_relative_reference_sha256=_relative_reference_digest(relative_path),
            regular_file_identity=_regular_file_identity(actual_status),
            content_sha256=digest,
            byte_count=len(payload),
            schema_version=_WRITE_RECEIPT_SCHEMA_VERSION,
        )
    except ParserSourceError:
        raise
    except OSError as exc:
        raise ParserSourceError(
            "PARSER.SOURCE.WRITE_FAILED",
            "atomic rooted byte replacement failed before a receipt was issued",
        ) from exc
    finally:
        if temporary_descriptor is not None:
            try:
                os.close(temporary_descriptor)
            except OSError:
                pass
        if (
            not replaced
            and temporary_name is not None
            and parent_descriptor is not None
        ):
            try:
                os.unlink(temporary_name, dir_fd=parent_descriptor)
            except OSError:
                pass
        if parent_descriptor is not None:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass
