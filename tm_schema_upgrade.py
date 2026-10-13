"""Schema-upgrade copy data plane and pending/reported artifact protocol.

Task 5.R2 boundary extraction: this module owns the v1->v2 copy data
plane, the durable backup/locator ``pending -> reported`` persistence
protocol (pending/report path family, exclusive creation/copy, strict
name/type/link/identity validation, fsync, promotion, pending sweep,
owned cleanup), the reusable strict locator file proof, and the pure
upgrade-candidate fact checks.  It never imports ``tm_sqlite_store`` or
``tm_migration``: schema DDL/constants and the canonical ancestry proof
implementation are consumed only through explicit immutable values,
callbacks and the narrow :class:`SchemaUpgradeCopyPlan`, and
authority-owned errors are produced through explicit error factories
instead of weakening exception classes, codes or retryable flags.

Authority retained by the owners:

* ``tm_sqlite_store`` keeps ``_SchemaUpgradeSnapshotTicket``,
  ``_SchemaUpgradeLocatorSnapshot``, ``ResourceStoreCoordinator``, ticket
  mint/retire/release/detach, every lease/drain/state transition,
  activation guards, cold-recovery root selection, the canonical
  ancestry implementation and general schema/store authority.  The
  coordinator-guard store reads (``_schema_upgrade_head_revision``,
  ``_read_schema_upgrade_marker`` and the ``_capture_activation_file``
  backed db capture) stay with that authority.
* ``tm_migration`` keeps ``MigrationPreflightError``, the public
  ``TMMigrationService`` entry points and the success/failure
  orchestration and report construction; generic explicit-import
  recovery candidate selection stays there, delegating its low-level
  strict file proof to this module.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePath
import sqlite3
import stat
from typing import Literal, Protocol

from tm_activation_journal import (ActivationPreparationError, _ActivationFileIdentity,
    _CallerHeldPosixActivationBorrow, _with_owned_close_facts)
from platform_fs_contracts import (BoundExistingFileMutationGuard, CandidateContentFacts, DirectoryObservationCancellation,
    DirectoryObservationLimits, ExistingFileMutationGuard, LockedRootDirectoryObservation,
    MutableFileReservation, OpaqueAuthority, PlatformFileError, PosixStageHandoff, PublishMode)
from tm_contracts import (
    CanonicalResourceIdentity,
    MutableStageRef,
    SnapshotKind,
    SnapshotReceipt,
)

_NATIVE_PATH_TYPE = type(Path())


class SchemaUpgradeIO:
    """Schema data-plane borrow; the coordinator retains root, W1 and state."""

    def __init__(self, borrow: _CallerHeldPosixActivationBorrow) -> None:
        self.borrow: _CallerHeldPosixActivationBorrow = borrow
        self.recovery_completed: bool = False
        self._sql_paths: set[Path] = set()
        self.reservations: dict[Path, MutableFileReservation] = {}
        self._contexts: dict[Path, AbstractContextManager[None]] = {}
        self._connections: dict[Path, list[sqlite3.Connection]] = {}
        self._pending_raw: list[OpaqueAuthority] | None = None
        self._pending_removals: list[tuple[Path, MutableFileReservation]] = []
        self._close_failed = False
        self.published_generation: int | None = None

    @property
    def recovery_required(self) -> bool:
        return self._pending_raw is not None or self._close_failed

    def require_raw_idle(self) -> None:
        if self._sql_paths or self.recovery_required:
            raise ActivationPreparationError("ACTIVATION.RECOVERY_REQUIRED", retryable=True)

    def capture(self, path: Path, *, synchronize: bool = False, expected_guard: BoundExistingFileMutationGuard | None = None):
        self.require_raw_idle()
        if path in self._sql_paths:
            raise ActivationPreparationError("ACTIVATION.RESERVATION_MISMATCH", retryable=False)
        return self.borrow.capture(path, synchronize=synchronize,
            **({"expected_guard": expected_guard} if expected_guard is not None else {}))

    def remove(self, path: Path, identity: tuple[int, int]) -> None:
        self.require_raw_idle()
        self.borrow.remove(path, _ActivationFileIdentity(*identity))

    def pending(self, store_path: Path) -> list[Path]:
        return self.candidates(store_path, f".{store_path.name}.localcat-schema-upgrade.", ".pending")

    def candidates(self, store_path: Path, prefix: str, suffix: str) -> list[Path]:
        self.require_raw_idle()
        borrow = self.borrow
        borrow.require_path(store_path)
        if not isinstance(borrow.backend, LockedRootDirectoryObservation):
            raise ActivationPreparationError("ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE", retryable=False)
        entries = borrow.backend.observe_root_children(borrow.root, borrow.lease,
            DirectoryObservationLimits(maximum_depth=0, maximum_entries=4096, maximum_directory_handles=1),
            DirectoryObservationCancellation())
        borrow.reprove()
        return [store_path.parent / entry.name for entry in entries
                if entry.name.startswith(prefix) and entry.name.endswith(suffix)]

    def retain_connection(self, path: Path, connection: sqlite3.Connection) -> None:
        self._connections[path].append(connection)

    def connect(self, database: str, *, uri: bool, isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None) -> sqlite3.Connection:
        connection = sqlite3.connect(database, uri=uri, isolation_level=isolation_level)
        # Every call belongs to the currently open group. Keep exact objects
        # until their entire group can be closed before any raw guard.
        path = next(reversed(self._connections))
        self._connections[path].append(connection)
        return connection

    @contextmanager
    def sql(self, *paths: Path) -> Generator[None]:
        """Close the whole backup/statement group before its raw authorities."""
        if self.recovery_required:
            raise ActivationPreparationError("ACTIVATION.RECOVERY_REQUIRED", retryable=True)
        guards: list[BoundExistingFileMutationGuard] = []
        registered: list[Path] = []
        primary: BaseException | None = None
        try:
            for path in paths:
                self.borrow.require_path(path)
                if path in self._sql_paths or not isinstance(self.borrow.backend, ExistingFileMutationGuard):
                    raise ActivationPreparationError("ACTIVATION.RESERVATION_MISMATCH", retryable=False)
                guards.append(self.borrow.backend.guard_existing_for_mutation(self.borrow.root, PurePath(path.name)))
                self._sql_paths.add(path)
                self._connections[path] = []
                registered.append(path)
            yield
            for guard in guards:
                try:
                    guard.reprove()
                except BaseException:
                    self.borrow.fail()
                    raise
            self.borrow.reprove()
        except BaseException as error:
            primary = error
            raise
        finally:
            connections = [connection for path in registered for connection in self._connections.get(path, [])]
            def release_sql_paths():
                for path in registered:
                    self._connections.pop(path, None)
                self._sql_paths.difference_update(registered)
            def finish_connections():
                first = None
                for connection in reversed(connections):
                    try:
                        connection.close()
                    except BaseException as error:
                        if first is None:
                            first = error
                        else:
                            first = _with_owned_close_facts(first, error, context="Schema SQL close failed")
                return first
            sql_error = finish_connections()
            if sql_error is not None:
                # A still-live SQL statement can retain POSIX record locks.
                # Recovery owns these exact connections AND their raw guards.
                retained: list[OpaqueAuthority] = list(guards)
                self._pending_raw = retained
                def retry():
                    error = finish_connections()
                    if error is not None:
                        return error
                    release_sql_paths()
                    # The original family invokes this callback only inside
                    # recovery's proven root/W1 bracket. Never reuse the old
                    # released borrow/lease or rebind the pathname.
                    try:
                        for path, reservation in tuple(self._pending_removals):
                            self.borrow.root.reprove_location()
                            self.borrow.root.unlink_owned(path.name, reservation.identity())
                            self._pending_removals.remove((path, reservation))
                    except BaseException as cleanup_error:
                        return cleanup_error
                    close_error = self.borrow.close_owned(*reversed(retained))
                    if close_error is None:
                        self._pending_raw = None
                    return close_error
                self.borrow._defer_close(retry)
                if primary is None:
                    raise sql_error
                enriched = _with_owned_close_facts(primary, sql_error, context="Schema SQL close failed")
                if enriched is not primary:
                    raise enriched from primary
            else:
                release_sql_paths()
                try:
                    error = self.borrow.close_owned(*reversed(guards), primary=primary)
                except BaseException:
                    self._close_failed = True
                    raise
                if error is not None:
                    self._close_failed = True
                    if primary is None:
                        raise error

    def recover_store(self, path: Path) -> None:
        """Let SQLite finish a legitimate hot journal before byte baselines."""
        with self.connection(path, mode="rw") as connection:
            connection.execute("PRAGMA schema_version").close()

    def close_reservations(self, *authorities: OpaqueAuthority | None, primary: BaseException | None = None) -> BaseException | None:
        if self._pending_raw is not None:
            self._pending_raw.extend(authority for authority in authorities if authority is not None)
            return None
        try:
            error = self.borrow.close_owned(*authorities, primary=primary)
        except BaseException:
            self._close_failed = True
            raise
        if error is not None:
            self._close_failed = True
        return error

    def reprove_sql(self, path: Path, purpose: str) -> None:
        del purpose
        self.borrow.require_path(path)

    def enter_sql(self, path: Path, purpose: str) -> None:
        del purpose
        context = self.sql(path)
        context.__enter__()
        self._contexts[path] = context

    def exit_sql(self, path: Path, purpose: str, *, primary: BaseException | None = None) -> None:
        del purpose
        context = self._contexts.pop(path)
        if primary is not None:
            _ = context.__exit__(type(primary), primary, primary.__traceback__)
        else:
            _ = context.__exit__(None, None, None)

    @contextmanager
    def connection(self, path: Path, *, mode: str = "ro") -> Generator[sqlite3.Connection]:
        with self.sql(path):
            connection = self.connect(f"{path.as_uri()}?mode={mode}", uri=True, isolation_level=None)
            try:
                self.borrow.reprove()
                yield connection
            finally:
                connection.close()

    def copy(self, source_path: Path, destination_path: Path) -> tuple[tuple[int, int], str]:
        self.require_raw_idle()
        borrow = self.borrow
        borrow.require_path(source_path)
        borrow.require_path(destination_path)
        reservation = self.reservations.get(destination_path)
        owned = reservation is None
        if reservation is None:
            reservation = borrow.backend.reserve_mutable_file(borrow.root, destination_path.name)
        primary: BaseException | None = None
        try:
            with self.sql(source_path, destination_path):
                source = self.connect(f"{source_path.as_uri()}?mode=ro", uri=True, isolation_level=None)
                try:
                    destination = self.connect(f"{destination_path.as_uri()}?mode=rw", uri=True, isolation_level=None)
                    try:
                        borrow.reprove()
                        destination.enable_load_extension(False)
                        destination.execute("PRAGMA journal_mode=DELETE").close()
                        destination.execute("PRAGMA synchronous=FULL").close()
                        source.backup(destination)
                    finally:
                        destination.close()
                finally:
                    source.close()
            proof = self.capture(destination_path, synchronize=True)
            _ = reservation.identity()
            return (proof.device, proof.inode), proof.sha256
        except BaseException as error:
            primary = error
            if owned and self._pending_raw is not None:
                self._pending_removals.append((destination_path, reservation))
            if owned and self._pending_raw is None:
                try:
                    borrow.reprove()
                    borrow.root.unlink_owned(destination_path.name, reservation.identity())
                except BaseException as cleanup_error:
                    enriched = _with_owned_close_facts(error, cleanup_error, context="Schema partial backup cleanup failed")
                    if enriched is not error:
                        raise enriched from error
            raise
        finally:
            if owned and self._pending_raw is not None:
                self._pending_raw.append(reservation)
            elif owned:
                close_error = borrow.close_owned(reservation, primary=primary)
                if primary is None and close_error is not None:
                    raise close_error

    def promote(self, path: Path, identity: tuple[int, int]) -> Path:
        self.require_raw_idle()
        borrow = self.borrow
        stable = _schema_upgrade_reported_path(path)
        borrow.require_path(path)
        if not borrow.exists(path):
            proof = self.capture(stable)
            if (proof.device, proof.inode) != identity:
                raise ActivationPreparationError("ACTIVATION.UPGRADE_PROMOTE_UNSAFE", retryable=False)
            return stable
        proof = self.capture(path)
        if (proof.device, proof.inode) != identity or borrow.exists(stable):
            raise ActivationPreparationError("ACTIVATION.UPGRADE_PROMOTE_UNSAFE", retryable=False)
        backend = borrow.backend
        if not isinstance(backend, PosixStageHandoff):
            raise ActivationPreparationError("ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE", retryable=False)
        content = CandidateContentFacts(proof.size, bytes.fromhex(proof.sha256))
        source = candidate = pending = None
        primary: BaseException | None = None
        try:
            source = backend.open_existing_stage(borrow.root, path.name, owner_lease=borrow.lease, expected_content=content)
            candidate = backend.handoff_stage(source, owner_lease=borrow.lease, expected_content=content)
            pending = borrow.root.begin_publish(candidate, stable.name, mode=PublishMode.CREATE_IF_ABSENT, lease=None)
            final = self.capture(stable)
            if final != proof:
                raise ActivationPreparationError("ACTIVATION.UPGRADE_PROMOTE_UNSAFE", retryable=False)
            _ = pending.terminal_reproof()
            borrow.reprove()
            return stable
        except BaseException as error:
            primary = error
            raise
        finally:
            close_error = borrow.close_owned(pending, candidate, source, primary=primary)
            if primary is None and close_error is not None:
                raise close_error



@dataclass(frozen=True)
class _CreatedFileIdentity:
    device: int
    inode: int


class _OwnedBackupTicket(Protocol):
    """Narrow structural view of the coordinator's opaque snapshot ticket.

    The ticket class itself stays with the coordinator authority in
    ``tm_sqlite_store``; owned cleanup only consumes the captured backup
    path and identity.
    """

    @property
    def backup_path(self) -> Path: ...

    @property
    def backup_identity(self) -> tuple[int, int]: ...


class _OwnedLocatorSnapshot(Protocol):
    """Narrow structural view of the coordinator's locator snapshot."""

    @property
    def path(self) -> Path: ...

    @property
    def identity(self) -> tuple[int, int]: ...


@dataclass(frozen=True)
class SchemaUpgradeCopyPlan:
    """Explicit immutable plan for the v1->v2 copy migration.

    The copy body never imports the store or migration owner.  Every
    authority-owned constant (schema DDL statements, approved digest
    table, upgrade meta marker, target schema version) and canonical
    ancestry callback is injected by ``tm_migration`` through this narrow
    plan, and authority-owned errors are raised through
    ``preflight_error_factory`` / ``ancestry_error_type`` so the moved
    body keeps the exact exception class, code and retryable semantics.
    """

    schema_statements: tuple[str, ...]
    fts5_statement: str
    approved_schema_digests: Mapping[bool, str]
    schema_upgrade_meta_key: str
    schema_upgrade_meta_value: str
    target_schema_version: int
    completed_origin_blocks: Callable[
        [sqlite3.Connection],
        Sequence[tuple[str, int, int]],
    ]
    revision_ancestry: Callable[..., object]
    unique_character_ngrams: Callable[[str, int], Sequence[str]]
    ancestry_error_type: type[Exception]
    preflight_error_factory: Callable[[str], BaseException]


def _activation_fsync_error_factory(
    _error: OSError,
) -> ActivationPreparationError:
    return ActivationPreparationError(
        "ACTIVATION.UPGRADE_BACKUP_FAILED",
        retryable=True,
    )


def _schema_upgrade_backup_path(
    store_path: Path,
    token: str,
) -> Path:
    """One collision-resistant *pending* recovery backup path for one upgrade.

    The name deliberately avoids the activation recovery glob
    ``.{name}.localcat-recovery.*.database.bak`` so recovery locators
    never confuse the two backup families.  The ``.pending`` suffix marks
    the file as an unexposed pending artifact: it is atomically renamed
    to the stable ``.bak`` reported suffix only immediately before a
    ``SchemaUpgradeReport`` or a failure ``RecoveryLocator`` is returned,
    so a crash can never turn an unreported full DB copy into permanent
    stable evidence, and the strictly validated pending family can be
    swept by the next fresh ticket mint or by cold recovery.
    """

    return (
        store_path.parent
        / f".{store_path.name}.localcat-schema-upgrade.{token}.bak.pending"
    ).absolute()


def _fsync_schema_upgrade_directory(
    path: Path,
    *,
    error_factory: Callable[[OSError], BaseException] = (
        _activation_fsync_error_factory
    ),
) -> None:
    """Fsync one schema-upgrade parent directory, fail-closed on error.

    The default ``error_factory`` reproduces the coordinator-side
    ``ACTIVATION.UPGRADE_BACKUP_FAILED`` error exactly; the migration
    side injects its own factory so the ``SCHEMA.COPY_FAILED`` mapping is
    preserved without weakening either error.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        os.fsync(descriptor)
    except OSError as error:
        raise error_factory(error) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _create_schema_upgrade_backup(
    source_path: Path,
    backup_path: Path,
    io: SchemaUpgradeIO | None = None,
) -> tuple[tuple[int, int], str]:
    """One consistent recovery backup via ``Connection.backup()``.

    The source is opened strictly read-only while the coordinator holds
    the resource drained (no leases), and the backup is written through
    the SQLite backup API into a fresh same-directory exclusively
    reserved regular file, then fsynced (file and parent).  The returned
    ``(device, inode)`` identity and SHA-256 digest describe the backup
    file itself; the backup is a valid reopenable old-schema store whose
    byte digest is deliberately not required to equal the active DB's
    byte digest.  Any failure removes the partially created backup and
    never touches the live canonical.
    """

    if io is not None:
        return io.copy(source_path, backup_path)


    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            backup_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | no_follow,
            0o600,
        )
        created = True
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_BACKUP_UNSAFE",
                retryable=False,
            )
        destination = sqlite3.connect(
            f"{backup_path.as_uri()}?mode=rw",
            uri=True,
            isolation_level=None,
        )
        try:
            destination.enable_load_extension(False)
            destination.execute("PRAGMA journal_mode=DELETE")
            destination.execute("PRAGMA synchronous=FULL")
            source = sqlite3.connect(
                f"{source_path.as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
            )
            try:
                source.backup(destination)
            finally:
                source.close()
        finally:
            destination.close()
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _fsync_schema_upgrade_directory(backup_path.parent)
        final = os.lstat(backup_path)
        if not stat.S_ISREG(final.st_mode) or final.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_BACKUP_UNSAFE",
                retryable=False,
            )
        backup_digest = _file_sha256_of_path(backup_path)
        return (final.st_dev, final.st_ino), backup_digest
    except ActivationPreparationError:
        _remove_partial_schema_upgrade_backup(backup_path)
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        _remove_partial_schema_upgrade_backup(backup_path)
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_BACKUP_FAILED",
            retryable=True,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _remove_partial_schema_upgrade_backup(backup_path: Path) -> None:
    """Best-effort removal of one exclusively created backup after failure."""

    try:
        observed = os.lstat(backup_path)
    except OSError:
        return
    if stat.S_ISREG(observed.st_mode) and observed.st_nlink == 1:
        try:
            backup_path.unlink()
        except OSError:
            pass


def _schema_upgrade_locator_snapshot_path(
    store_path: Path,
    token: str,
) -> Path:
    """One collision-resistant *pending* byte-exact locator snapshot path.

    The name deliberately avoids the schema-upgrade backup glob
    ``.{name}.localcat-schema-upgrade.*.bak`` and the activation recovery
    glob ``.{name}.localcat-recovery.*.database.bak`` so backup counts and
    recovery locators never confuse the two artifact families.  The
    ``.pending`` suffix marks the unexposed pending state: the snapshot
    is atomically renamed to the stable ``.locator`` reported suffix only
    when a failure exposes it as a ``RecoveryLocator``, and is otherwise
    strictly removed on success or on any failure that does not expose it.
    """

    return (
        store_path.parent
        / f".{store_path.name}.localcat-schema-upgrade.{token}.locator.pending"
    ).absolute()


def _create_schema_upgrade_locator_snapshot(
    source_path: Path,
    snapshot_path: Path,
    io: SchemaUpgradeIO | None = None,
) -> tuple[tuple[int, int], str]:
    """One strict raw byte-exact copy of the drained prior store.

    The source is opened no-follow while the coordinator holds the
    resource drained, and every byte is copied into a fresh same-directory
    ``O_EXCL`` regular single-link file that is fsynced (file and parent).
    The returned identity/digest describe the snapshot file itself and the
    digest is byte-identical to the drained source, so the snapshot can
    honestly back a ``RecoveryLocator`` bound to the prior store digest.
    Any failure removes the partial snapshot and never touches the live
    canonical.
    """

    if io is not None:
        borrow = io.borrow
        borrow.require_path(source_path)
        borrow.require_path(snapshot_path)
        opened = borrow.backend.open_regular(borrow.root, PurePath(source_path.name))
        candidate = pending = None
        publication_entered = False
        primary: BaseException | None = None
        try:
            facts = opened.content_facts()
            candidate = borrow.root.create_candidate(snapshot_path.name + ".tmp", private=False)
            def chunks():
                for offset in range(0, facts.snapshot.byte_count, 65536):
                    yield opened.read_at(offset, 65536, facts.snapshot)
            _ = candidate.write_chunks(chunks(), maximum_bytes=facts.snapshot.byte_count)
            _ = candidate.flush_content()
            borrow.reprove()
            publication_entered = True
            pending = borrow.root.begin_publish(candidate, snapshot_path.name, mode=PublishMode.CREATE_IF_ABSENT, lease=None)
            proof = io.capture(snapshot_path)
            _ = pending.terminal_reproof()
            return (proof.device, proof.inode), proof.sha256
        except BaseException as error:
            primary = error
            if candidate is not None and not publication_entered:
                try:
                    borrow.reprove()
                    borrow.root.unlink_owned(snapshot_path.name + ".tmp", candidate.identity())
                except BaseException as cleanup_error:
                    enriched = _with_owned_close_facts(error, cleanup_error, context="Schema partial locator cleanup failed")
                    if enriched is not error:
                        raise enriched from error
            raise
        finally:
            closing = borrow.close_owned(pending, candidate, opened, primary=primary)
            if primary is None and closing is not None:
                raise closing


    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    source_descriptor = -1
    destination_descriptor = -1
    identity: tuple[int, int] | None = None
    try:
        source_descriptor = os.open(source_path, os.O_RDONLY | no_follow)
        source_observed = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(source_observed.st_mode)
            or source_observed.st_nlink != 1
        ):
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_SNAPSHOT_UNSAFE",
                retryable=False,
            )
        destination_descriptor = os.open(
            snapshot_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | no_follow,
            0o600,
        )
        destination_observed = os.fstat(destination_descriptor)
        if (
            not stat.S_ISREG(destination_observed.st_mode)
            or destination_observed.st_nlink != 1
        ):
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_SNAPSHOT_UNSAFE",
                retryable=False,
            )
        identity = (
            destination_observed.st_dev,
            destination_observed.st_ino,
        )
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written <= 0:
                    raise OSError("locator snapshot write made no progress")
                view = view[written:]
        os.fsync(destination_descriptor)
        os.close(destination_descriptor)
        destination_descriptor = -1
        _fsync_schema_upgrade_directory(snapshot_path.parent)
        final = os.lstat(snapshot_path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != identity
        ):
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_SNAPSHOT_UNSAFE",
                retryable=False,
            )
        digest = _file_sha256_of_path(snapshot_path)
        return identity, digest
    except ActivationPreparationError:
        _remove_partial_schema_upgrade_backup(snapshot_path)
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        _remove_partial_schema_upgrade_backup(snapshot_path)
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_SNAPSHOT_FAILED",
            retryable=True,
        ) from error
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)


def _remove_owned_schema_upgrade_artifact(
    path: Path,
    identity: tuple[int, int],
    io: SchemaUpgradeIO | None = None,
) -> None:
    """Strict best-effort removal of one owned schema-upgrade artifact."""

    if io is not None:
        io.remove(path, identity)
        return


    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(observed.st_mode)
        and observed.st_nlink == 1
        and (observed.st_dev, observed.st_ino) == identity
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _schema_upgrade_reported_path(pending_path: Path) -> Path:
    """The stable reported sibling of one pending schema-upgrade artifact.

    The reported suffix is the pending name without the ``.pending``
    marker: ``*.bak.pending`` promotes to the stable success ``*.bak``
    and ``*.locator.pending`` promotes to the stable exposed-failure
    ``*.locator``.  Stable reported artifacts are never swept by pending
    cleanup, so an exposed locator or reported success backup survives
    every later retry and cold recovery.
    """

    if type(pending_path) is not _NATIVE_PATH_TYPE:
        raise TypeError("pending artifact path must be pathlib.Path")
    if not pending_path.name.endswith(".pending"):
        raise ValueError("schema-upgrade artifact path is not pending")
    return pending_path.with_name(pending_path.name[: -len(".pending")])


def _promote_schema_upgrade_artifact(
    path: Path,
    identity: tuple[int, int],
    io: SchemaUpgradeIO | None = None,
) -> Path:
    """Atomically promote one owned pending artifact to its stable suffix.

    Only the exact regular single-link file carrying the captured
    identity is renamed (parent fsynced); a missing pending file whose
    stable sibling still carries the captured identity is already
    promoted (idempotent cold-recovery replay), and a symlink, directory,
    multi-link, missing, or foreign inode is never renamed but fails
    closed so the caller stops instead of exposing or deleting an
    unaccounted artifact.
    """

    if io is not None:
        return io.promote(path, identity)


    if (
        type(identity) is not tuple
        or len(identity) != 2
        or type(identity[0]) is not int
        or type(identity[1]) is not int
    ):
        raise TypeError("pending artifact identity is invalid")
    stable_path = _schema_upgrade_reported_path(path)
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        try:
            stable_observed = os.lstat(stable_path)
        except FileNotFoundError:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
                retryable=False,
            )
        if (
            not stat.S_ISREG(stable_observed.st_mode)
            or stable_observed.st_nlink != 1
            or (stable_observed.st_dev, stable_observed.st_ino) != identity
        ):
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
                retryable=False,
            )
        return stable_path
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
            retryable=False,
        )
    try:
        stable_observed = os.lstat(stable_path)
    except FileNotFoundError:
        stable_observed = None
    if stable_observed is not None:
        # A coexisting stable sibling can never be the same single-link
        # file (that would make the pending entry multi-link), so it is a
        # foreign artifact and must never be replaced by the rename.
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
            retryable=False,
        )
    try:
        os.rename(path, stable_path)
        _fsync_schema_upgrade_directory(path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
            retryable=False,
        ) from error
    return stable_path


def _pending_schema_upgrade_family(store_path: Path, io: SchemaUpgradeIO | None = None) -> list[Path]:
    """The deterministic unexposed pending schema-upgrade artifact names.

    Only the ``.{name}.localcat-schema-upgrade.*.pending`` family is ever
    a crash-orphan candidate; stable reported ``*.bak`` / ``*.locator``
    artifacts and the journal-owned activation recovery glob are never
    matched.
    """

    if io is not None:
        return io.pending(store_path)
    return sorted(
        store_path.parent.glob(
            f".{store_path.name}.localcat-schema-upgrade.*.pending"
        ),
        key=str,
    )


def _require_owned_pending_schema_upgrade_name(
    path: Path,
    store_path: Path,
) -> None:
    """Strict deterministic-family validation for one pending name.

    A pending entry must be ``.{store name}.localcat-schema-upgrade.``
    followed by a 32-hex token and exactly ``.bak.pending`` or
    ``.locator.pending``; any other name is foreign and fails closed.
    """

    prefix = f".{store_path.name}.localcat-schema-upgrade."
    name = path.name
    if not name.startswith(prefix):
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PENDING_UNSAFE",
            retryable=False,
        )
    remainder = name[len(prefix):]
    for stable_suffix in (".bak", ".locator"):
        pending_suffix = f"{stable_suffix}.pending"
        if remainder.endswith(pending_suffix):
            token = remainder[: -len(pending_suffix)]
            if (
                len(token) == 32
                and all(ch in "0123456789abcdef" for ch in token)
            ):
                return
    raise ActivationPreparationError(
        "ACTIVATION.UPGRADE_PENDING_UNSAFE",
        retryable=False,
    )


def _sweep_pending_schema_upgrade_artifacts(store_path: Path, io: SchemaUpgradeIO | None = None) -> None:
    """Strictly remove crash-orphan pending schema-upgrade artifacts.

    Every ``*.pending`` entry of the deterministic family is validated
    (name pattern plus regular single-link file) immediately before it is
    unlinked; a symlink, directory, multi-link, or foreign entry is never
    unlinked but fails closed so the caller stops instead of deleting an
    unaccounted file.  Stable reported artifacts are never matched, so an
    exposed locator or reported success backup always survives cleanup.
    The parent directory is fsynced once after all removals.
    """

    candidates = _pending_schema_upgrade_family(store_path, io=io)
    if not candidates:
        return
    for candidate in candidates:
        _require_owned_pending_schema_upgrade_name(candidate, store_path)
        if io is not None:
            try:
                proof = io.capture(candidate)
            except (PlatformFileError, OSError) as error:
                raise ActivationPreparationError("ACTIVATION.UPGRADE_PENDING_UNSAFE", retryable=False) from error
            io.remove(candidate, (proof.device, proof.inode))
            continue
        try:
            observed = os.lstat(candidate)
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_PENDING_UNSAFE",
                retryable=False,
            ) from error
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_PENDING_UNSAFE",
                retryable=False,
            )
        try:
            candidate.unlink()
        except OSError as error:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_PENDING_CLEANUP_FAILED",
                retryable=True,
            ) from error
    if io is None:
        _fsync_schema_upgrade_directory(store_path.parent)


def _promote_pending_schema_upgrade_backup(
    store_path: Path,
    io: SchemaUpgradeIO | None = None,
) -> Path | None:
    """Promote the one pending Connection.backup to the stable suffix.

    A completed cold recovery of a schema upgrade (a recovered v2 store
    mints no further upgrade ticket) must still retain exactly one stable
    reported backup: the sole pending ``.bak.pending`` is atomically
    renamed to ``.bak`` after strict deterministic-family and
    regular-single-link validation.  No pending backup is a no-op; more
    than one pending backup or any unsafe entry fails closed.
    """

    candidates = [
        candidate
        for candidate in _pending_schema_upgrade_family(store_path, io=io)
        if candidate.name.endswith(".bak.pending")
    ]
    if not candidates:
        return None
    if len(candidates) != 1:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PENDING_UNSAFE",
            retryable=False,
        )
    candidate = candidates[0]
    _require_owned_pending_schema_upgrade_name(candidate, store_path)
    if io is not None:
        try:
            proof = io.capture(candidate)
            return io.promote(candidate, (proof.device, proof.inode))
        except (PlatformFileError, OSError) as error:
            raise ActivationPreparationError("ACTIVATION.UPGRADE_PENDING_UNSAFE", retryable=False) from error
    try:
        observed = os.lstat(candidate)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PENDING_UNSAFE",
            retryable=False,
        ) from error
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PENDING_UNSAFE",
            retryable=False,
        )
    stable_path = _schema_upgrade_reported_path(candidate)
    try:
        stable_observed = os.lstat(stable_path)
    except FileNotFoundError:
        stable_observed = None
    if stable_observed is not None:
        # The stable reported suffix is never overwritten: a coexisting
        # stable sibling is either already-promoted evidence (never to be
        # replaced) or a foreign artifact, so the cold completion fails
        # closed instead of clobbering it.
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
            retryable=False,
        )
    try:
        os.rename(candidate, stable_path)
        _fsync_schema_upgrade_directory(candidate.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_PROMOTE_UNSAFE",
            retryable=False,
        ) from error
    return stable_path


def _finish_cold_schema_upgrade_pending(
    store_path: Path,
    *,
    completed: bool,
    io: SchemaUpgradeIO | None = None,
) -> None:
    """Deterministic pending-artifact resolution after cold recovery.

    A completed activation retains the Design-required schema success
    backup as exactly one stable reported ``.bak`` (promoting the pending
    copy); any cancelled or rolled-back outcome leaves no pending or
    stable hidden full-copy artifacts.  Remaining pending entries (for
    example an abandoned byte-exact locator snapshot) are strictly
    swept, and stable reported artifacts always survive.
    """

    if completed:
        _promote_pending_schema_upgrade_backup(store_path, io=io)
    _sweep_pending_schema_upgrade_artifacts(store_path, io=io)


def _remove_schema_upgrade_backup(
    ticket: _OwnedBackupTicket,
    io: SchemaUpgradeIO | None = None,
) -> None:
    """Safely unlink the exact owned recovery backup of one retired ticket.

    Only the file created by the ticket (regular single-link file with the
    captured identity) is removed; a missing file is already gone and a
    foreign inode is never unlinked but fails closed so the caller can
    stop instead of leaving an unaccounted full DB copy.
    """

    if io is not None:
        io.remove(ticket.backup_path, ticket.backup_identity)
        return


    try:
        observed = os.lstat(ticket.backup_path)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != ticket.backup_identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_BACKUP_CLEANUP_FAILED",
            retryable=True,
        )
    try:
        ticket.backup_path.unlink()
        _fsync_schema_upgrade_directory(ticket.backup_path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_BACKUP_CLEANUP_FAILED",
            retryable=True,
        ) from error


def _remove_schema_upgrade_locator_snapshot(
    snapshot: _OwnedLocatorSnapshot,
    io: SchemaUpgradeIO | None = None,
) -> None:
    """Strictly delete one coordinator-captured locator snapshot."""

    if io is not None:
        io.remove(snapshot.path, snapshot.identity)
        return


    try:
        observed = os.lstat(snapshot.path)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != snapshot.identity
    ):
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_SNAPSHOT_INVALID",
            retryable=False,
        )
    try:
        snapshot.path.unlink()
        _fsync_schema_upgrade_directory(snapshot.path.parent)
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.UPGRADE_SNAPSHOT_CLEANUP_FAILED",
            retryable=True,
        ) from error


def _file_sha256_of_path(path: Path) -> str:
    """One strict no-follow SHA-256 of an existing regular single-link file."""

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = os.open(path, os.O_RDONLY | no_follow)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.UPGRADE_BACKUP_UNSAFE",
                retryable=False,
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _strict_locator_proof(
    path: Path,
    expected_digest: str,
    io: SchemaUpgradeIO | None = None,
) -> tuple[tuple[int, int], str] | None:
    """One strict prove-or-reject check for a recovery locator candidate.

    A candidate is accepted only when it opens ``O_NOFOLLOW``, ``fstat``
    reports a regular single-link file, its bytes hash to
    ``expected_digest``, and a terminal ``lstat`` still reports the same
    device/inode/type/nlink.  Symlink, directory, multi-link, missing,
    swapped inode, unreadable, or digest-mismatch candidates are rejected
    (``None``) and are never exposed as locators.
    """

    if io is not None:
        try:
            proof = io.capture(path)
            return ((proof.device, proof.inode), proof.sha256) if proof.sha256 == expected_digest else None
        except (OSError, PlatformFileError):
            return None


    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            return None
        identity = (observed.st_dev, observed.st_ino)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        if digest.hexdigest() != expected_digest:
            return None
        os.close(descriptor)
        descriptor = -1
        final = os.lstat(path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != identity
        ):
            return None
        return identity, digest.hexdigest()
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _schema_version_of_store(
    store_path: Path,
    *,
    preflight_error_factory: Callable[[str], BaseException],
    io: SchemaUpgradeIO | None = None,
) -> int:
    """Read the meta schema version of one existing store read-only."""

    with io.sql(store_path) if io is not None else nullcontext():
        connection = _open_legacy_read_connection(store_path, io=io)
        try:
            rows = connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'schema_version'"
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise preflight_error_factory(
                "SCHEMA.SCHEMA_UNREADABLE"
            ) from error
        finally:
            connection.close()
    if len(rows) != 1:
        raise preflight_error_factory("SCHEMA.SCHEMA_UNREADABLE")
    try:
        version = int(str(rows[0][0]))
    except (TypeError, ValueError) as error:
        raise preflight_error_factory(
            "SCHEMA.SCHEMA_UNREADABLE"
        ) from error
    return version


def _upgrade_source_ref(
    identity: CanonicalResourceIdentity,
    store_path: Path,
) -> MutableStageRef:
    """One inspection ref over the active old-schema store path.

    The inspection only reads the database, so the structural manifest
    temporary is a distinct adjacent placeholder (the published manifest
    path is deliberately never reused as a stage temporary).
    """

    return MutableStageRef(
        stage_id="stage.schema-upgrade.source",
        resource_identity=identity,
        staged_db_path=store_path,
        manifest_temp_path=store_path.with_name(
            f"{store_path.name}.localcat-schema-upgrade.inspect.tmp"
        ),
    )


def _open_legacy_read_connection(
    database_path: Path,
    io: SchemaUpgradeIO | None = None,
) -> sqlite3.Connection:
    """One strictly read-only connection that never mutates the source."""

    connection = (io.connect if io is not None else sqlite3.connect)(
        f"{database_path.as_uri()}?mode=ro",
        uri=True,
        isolation_level=None,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _read_active_activation_digest(
    store_path: Path,
    *,
    preflight_error_factory: Callable[[str], BaseException],
    io: SchemaUpgradeIO | None = None,
) -> str:
    """Read the ACTIVE old-schema canonical's activation digest.

    The digest expectation is read from the store's own durable meta
    (never guessed) so the strict ACTIVE inspection binds the exact
    published generation; any missing or malformed digest fails closed.
    """

    with io.sql(store_path) if io is not None else nullcontext():
        connection = _open_legacy_read_connection(store_path, io=io)
        try:
            rows = connection.execute(
                "SELECT value FROM tm_meta "
                "WHERE key = 'activation_digest'"
            ).fetchall()
        finally:
            connection.close()
    if (
        len(rows) != 1
        or type(rows[0][0]) is not str
        or len(rows[0][0]) != 64
        or any(
            character not in "0123456789abcdef"
            for character in rows[0][0]
        )
    ):
        raise preflight_error_factory("SCHEMA.UPGRADE_UNSUPPORTED")
    return str(rows[0][0])


def _read_legacy_snapshot_facts(
    store_path: Path,
    *,
    canonical_store_id: str,
    preflight_error_factory: Callable[[str], BaseException],
    io: SchemaUpgradeIO | None = None,
) -> tuple[SnapshotReceipt, SnapshotKind]:
    """Read the one old-schema receipt and binding kind the copy re-publishes.

    The receipt fields and the binding's ``snapshot_kind`` are read
    exactly as the v1 ledger recorded them; the binding itself is deleted
    from the candidate and re-published identically by activation, so the
    manifest temporary must carry the original kind.
    """

    with io.sql(store_path) if io is not None else nullcontext():
        connection = _open_legacy_read_connection(store_path, io=io)
        try:
            receipt_rows = connection.execute(
                "SELECT snapshot_id, resource_id, canonical_store_id, "
                "exported_revision, jsonl_digest, record_count, format_version "
                "FROM tm_snapshot_receipt ORDER BY snapshot_id"
            ).fetchall()
            binding_rows = connection.execute(
                "SELECT snapshot_kind FROM tm_snapshot_binding "
                "WHERE binding_id = 1"
            ).fetchall()
        finally:
            connection.close()
    if len(receipt_rows) != 1:
        raise preflight_error_factory("SCHEMA.UPGRADE_UNSUPPORTED")
    row = receipt_rows[0]
    scalar_values = row[:3] + (row[4], row[6])
    if any(type(value) is not str for value in scalar_values):
        raise preflight_error_factory("SCHEMA.UPGRADE_UNSUPPORTED")
    if type(row[3]) is not int or type(row[5]) is not int:
        raise preflight_error_factory("SCHEMA.UPGRADE_UNSUPPORTED")
    receipt = SnapshotReceipt(
        snapshot_id=str(row[0]),
        resource_id=str(row[1]),
        canonical_store_id=str(row[2]),
        exported_revision=int(row[3]),
        jsonl_digest=str(row[4]),
        record_count=int(row[5]),
        format_version=str(row[6]),
    )
    if receipt.canonical_store_id != canonical_store_id:
        raise preflight_error_factory("SCHEMA.UPGRADE_UNSUPPORTED")
    if len(binding_rows) != 1 or type(binding_rows[0][0]) is not str:
        raise preflight_error_factory("SCHEMA.UPGRADE_UNSUPPORTED")
    try:
        manifest_kind = SnapshotKind(str(binding_rows[0][0]))
    except (TypeError, ValueError) as error:
        raise preflight_error_factory(
            "SCHEMA.UPGRADE_UNSUPPORTED"
        ) from error
    return receipt, manifest_kind


def _remove_owned_schema_upgrade_backup(
    path: Path,
    identity: tuple[int, int],
    *,
    preflight_error_factory: Callable[[str], BaseException],
    fsync_error_factory: Callable[[OSError], BaseException],
    io: SchemaUpgradeIO | None = None,
) -> None:
    """Safely unlink one exact owned schema-upgrade recovery backup.

    Only a regular single-link file carrying the captured identity is
    removed; a missing file is already gone and a foreign inode is never
    unlinked but fails closed so the caller can stop instead of leaving an
    unaccounted full DB copy.  The parent directory is fsynced after the
    unlink.
    """

    if io is not None:
        io.remove(path, identity)
        return


    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != identity
    ):
        raise preflight_error_factory("SCHEMA.BACKUP_CLEANUP_UNSAFE")
    try:
        path.unlink()
        _fsync_schema_upgrade_directory(
            path.parent,
            error_factory=fsync_error_factory,
        )
    except OSError as error:
        raise preflight_error_factory(
            "SCHEMA.BACKUP_CLEANUP_FAILED"
        ) from error


def _copy_store_into_stage(
    source_path: Path,
    destination_path: Path,
    *,
    preflight_error_type: type[BaseException],
    preflight_error_factory: Callable[[str], BaseException],
    fsync_error_factory: Callable[[OSError], BaseException],
    io: SchemaUpgradeIO | None = None,
) -> _CreatedFileIdentity:
    """One consistent old-schema copy into a fresh exclusively reserved file.

    The source is opened strictly read-only and copied through the SQLite
    backup API into a same-directory fresh ``O_EXCL`` regular file, then
    fsynced (file and parent).  The copy is a valid reopenable old-schema
    store; its byte digest is deliberately not required to equal the live
    DB's digest (``Connection.backup()`` may normalize page layout).  Any
    failure removes the partial copy and never touches the live canonical.
    """

    if io is not None:
        identity, _digest = io.copy(source_path, destination_path)
        return _CreatedFileIdentity(*identity)


    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    created = False
    try:
        descriptor = os.open(
            destination_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | no_follow,
            0o600,
        )
        created = True
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise preflight_error_factory("SCHEMA.COPY_UNSAFE")
        identity = _CreatedFileIdentity(observed.st_dev, observed.st_ino)
        source = sqlite3.connect(
            f"{source_path.as_uri()}?mode=ro",
            uri=True,
            isolation_level=None,
        )
        try:
            destination = sqlite3.connect(
                f"{destination_path.as_uri()}?mode=rw",
                uri=True,
                isolation_level=None,
            )
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        _fsync_schema_upgrade_directory(
            destination_path.parent,
            error_factory=fsync_error_factory,
        )
        final = os.lstat(destination_path)
        if (
            not stat.S_ISREG(final.st_mode)
            or (final.st_dev, final.st_ino) != (identity.device, identity.inode)
        ):
            raise preflight_error_factory("SCHEMA.COPY_UNSAFE")
        return identity
    except preflight_error_type:
        if created:
            try:
                destination_path.unlink()
            except OSError:
                pass
        raise
    except (OSError, sqlite3.DatabaseError) as error:
        if created:
            try:
                destination_path.unlink()
            except OSError:
                pass
        raise preflight_error_factory("SCHEMA.COPY_FAILED") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ddl_for(
    statements: tuple[str, ...],
    prefix: str,
) -> str:
    matches = [
        statement
        for statement in statements
        if statement.lstrip().startswith(prefix)
    ]
    if len(matches) != 1:
        raise ValueError("schema DDL statement set is inconsistent")
    return matches[0]


def _migrate_schema_copy(
    connection: sqlite3.Connection,
    *,
    fts5_available: bool,
    plan: SchemaUpgradeCopyPlan,
) -> None:
    """Migrate one copied pre-v2 stage to the current schema in place.

    The rebuild renames the legacy tables aside, recreates the v2 shape,
    derives each completed batch's ``completed_revision`` strictly from
    the proven record-block/origin-ordinal order (never batch-id
    sorting), copies every record verbatim (including provenance,
    context, usage and lineage), rebuilds ``tm_gram`` and, when
    available, ``tm_fts`` from ``tm_record.source_fold_v1``, re-issues
    the single receipt, removes the binding (re-published identically by
    activation), and advances meta to the current schema version and
    approved digest with the durable schema-upgrade origin marker.  The
    whole rebuild is one transaction, so a failed upgrade leaves only
    the untouched original store and its recovery backup.
    """

    connection.execute("BEGIN IMMEDIATE")
    try:
        try:
            head_rows = connection.execute(
                "SELECT value FROM tm_meta WHERE key = 'head_revision'"
            ).fetchall()
            if len(head_rows) != 1:
                raise plan.ancestry_error_type(
                    "STORE.REVISION_ANCESTRY_MISMATCH"
                )
            head_revision = int(str(head_rows[0][0]))
            record_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM tm_record"
                ).fetchone()[0]
            )
            completed_blocks = plan.completed_origin_blocks(connection)
            _ = plan.revision_ancestry(
                connection,
                head_revision=head_revision,
                record_count=record_count,
            )
        except plan.ancestry_error_type as error:
            raise plan.preflight_error_factory(
                "SCHEMA.ANCESTRY_UNPROVABLE"
            ) from error
        except (TypeError, ValueError) as error:
            raise plan.preflight_error_factory(
                "SCHEMA.ANCESTRY_UNPROVABLE"
            ) from error
        revision_by_batch = {
            batch_id: revision
            for batch_id, revision, _count in completed_blocks
        }
        for statement in (
            "DROP INDEX idx_tm_exact",
            "DROP INDEX idx_tm_context_speaker",
            "DROP INDEX idx_tm_gram_lookup",
        ):
            connection.execute(statement)
        connection.execute(
            "ALTER TABLE tm_origin_batch RENAME TO tm_origin_batch_legacy"
        )
        connection.execute(
            "ALTER TABLE tm_record RENAME TO tm_record_legacy"
        )
        connection.execute(
            "ALTER TABLE tm_gram RENAME TO tm_gram_legacy"
        )
        for statement in (
            _ddl_for(
                plan.schema_statements,
                "CREATE TABLE tm_origin_batch (",
            ),
            _ddl_for(plan.schema_statements, "CREATE TABLE tm_record ("),
            _ddl_for(plan.schema_statements, "CREATE TABLE tm_gram ("),
            _ddl_for(
                plan.schema_statements,
                "CREATE TABLE tm_candidate_block (",
            ),
            _ddl_for(
                plan.schema_statements,
                "CREATE TABLE tm_gram_block_max (",
            ),
        ):
            connection.execute(statement)
        legacy_batch_cursor = connection.execute(
            "SELECT batch_id, kind, source_digest, source_path, status, "
            "valid_count, invalid_count, duplicate_source_count, "
            "created_at FROM tm_origin_batch_legacy ORDER BY batch_id"
        )
        for batch in legacy_batch_cursor:
            batch_id = str(batch[0])
            connection.execute(
                "INSERT INTO tm_origin_batch("
                "batch_id, kind, source_digest, source_path, status, "
                "valid_count, invalid_count, duplicate_source_count, "
                "completed_revision, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    str(batch[1]),
                    batch[2],
                    batch[3],
                    str(batch[4]),
                    int(batch[5]),
                    int(batch[6]),
                    int(batch[7]),
                    revision_by_batch.get(batch_id),
                    str(batch[8]),
                ),
            )
        record_rows: list[tuple[object, ...]] = []
        legacy_record_cursor = connection.execute(
            "SELECT record_id, source_raw, target_raw, source_fold_v1, "
            "speaker_raw, context_prev_raw, context_next_raw, "
            "file_source, provenance_json, legacy_line_no, usage_count, "
            "last_used, origin_batch_id, origin_ordinal "
            "FROM tm_record_legacy ORDER BY record_id"
        )
        for row in legacy_record_cursor:
            folded_source = str(row[3])
            record_rows.append((*row[:4], len(folded_source), *row[4:]))
            if len(record_rows) >= 5000:
                connection.executemany(
                    "INSERT INTO tm_record("
                    "record_id, source_raw, target_raw, source_fold_v1, "
                    "source_fold_length, speaker_raw, context_prev_raw, "
                    "context_next_raw, file_source, provenance_json, "
                    "legacy_line_no, usage_count, last_used, "
                    "origin_batch_id, origin_ordinal) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    record_rows,
                )
                record_rows.clear()
        if record_rows:
            connection.executemany(
                "INSERT INTO tm_record("
                "record_id, source_raw, target_raw, source_fold_v1, "
                "source_fold_length, speaker_raw, context_prev_raw, "
                "context_next_raw, file_source, provenance_json, "
                "legacy_line_no, usage_count, last_used, "
                "origin_batch_id, origin_ordinal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                record_rows,
            )
        required_sizes = (1, 2) if fts5_available else (1, 2, 3)
        gram_rows: list[tuple[int, str, int, int]] = []
        current_block_id: int | None = None
        block_lengths: list[int] = []
        block_maxima: dict[tuple[int, str], int] = {}

        def flush_proof_block() -> None:
            if current_block_id is None:
                return
            first_record_id = current_block_id * 256 + 1
            connection.execute(
                "INSERT INTO tm_candidate_block("
                "block_id, first_record_id, last_record_id, record_count, "
                "min_source_fold_length, max_source_fold_length) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    current_block_id,
                    first_record_id,
                    first_record_id + 255,
                    len(block_lengths),
                    min(block_lengths),
                    max(block_lengths),
                ),
            )
            connection.executemany(
                "INSERT INTO tm_gram_block_max("
                "gram_size, gram, block_id, max_term_frequency) "
                "VALUES (?, ?, ?, ?)",
                tuple(
                    (size, gram, current_block_id, frequency)
                    for (size, gram), frequency in sorted(block_maxima.items())
                ),
            )

        record_cursor = connection.execute(
            "SELECT record_id, source_fold_v1 FROM tm_record "
            "ORDER BY record_id"
        )
        for record_id, folded_source in record_cursor:
            folded_source = str(folded_source)
            record_id = int(record_id)
            next_block_id = (record_id - 1) // 256
            if current_block_id is not None and next_block_id != current_block_id:
                flush_proof_block()
                block_lengths.clear()
                block_maxima.clear()
            current_block_id = next_block_id
            block_lengths.append(len(folded_source))
            for gram_size in required_sizes:
                frequencies = Counter(
                    folded_source[offset : offset + gram_size]
                    for offset in range(
                        max(0, len(folded_source) - gram_size + 1)
                    )
                )
                for gram in plan.unique_character_ngrams(
                    folded_source,
                    gram_size,
                ):
                    term_frequency = frequencies[gram]
                    gram_rows.append(
                        (gram_size, gram, record_id, term_frequency)
                    )
                    if gram_size in {1, 2}:
                        key = (gram_size, gram)
                        block_maxima[key] = max(
                            block_maxima.get(key, 0), term_frequency
                        )
                    if len(gram_rows) >= 5000:
                        connection.executemany(
                            "INSERT INTO tm_gram("
                            "gram_size, gram, record_id, term_frequency) "
                            "VALUES (?, ?, ?, ?)",
                            gram_rows,
                        )
                        gram_rows.clear()
        if gram_rows:
            connection.executemany(
                "INSERT INTO tm_gram("
                "gram_size, gram, record_id, term_frequency) "
                "VALUES (?, ?, ?, ?)",
                gram_rows,
            )
        flush_proof_block()
        for table_name in (
            "tm_gram_legacy",
            "tm_record_legacy",
            "tm_origin_batch_legacy",
        ):
            connection.execute(f"DROP TABLE {table_name}")
        for statement in (
            _ddl_for(
                plan.schema_statements,
                "CREATE INDEX idx_tm_exact",
            ),
            _ddl_for(
                plan.schema_statements,
                "CREATE INDEX idx_tm_context_speaker",
            ),
            _ddl_for(
                plan.schema_statements,
                "CREATE INDEX idx_tm_gram_lookup",
            ),
            _ddl_for(
                plan.schema_statements,
                "CREATE INDEX idx_tm_gram_block_lookup",
            ),
        ):
            connection.execute(statement)
        if fts5_available:
            connection.execute("DROP TABLE tm_fts")
            connection.execute(plan.fts5_statement)
            connection.execute(
                "INSERT INTO tm_fts(source_fold_v1, record_id) "
                "SELECT source_fold_v1, record_id FROM tm_record "
                "ORDER BY record_id"
            )
        connection.execute(
            "UPDATE tm_snapshot_receipt SET status = 'issued'"
        )
        connection.execute("DELETE FROM tm_snapshot_binding")
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'schema_version'",
            (str(plan.target_schema_version),),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'schema_digest'",
            (plan.approved_schema_digests[fts5_available],),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'activation_status'",
            ("UNPUBLISHED",),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'generation'",
            ("0",),
        )
        connection.execute(
            "UPDATE tm_meta SET value = ? WHERE key = 'divergence_latched'",
            ("0",),
        )
        connection.execute(
            "DELETE FROM tm_meta WHERE key = 'activation_digest'"
        )
        connection.execute(
            "INSERT INTO tm_meta(key, value) VALUES (?, ?)",
            (plan.schema_upgrade_meta_key, plan.schema_upgrade_meta_value),
        )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
