"""Task 5.R3 snapshot artifact namespace, proof and handoff primitives.

Cluster F behavior-preserving boundary extraction: this module owns the
deterministic JSONL/manifest/temp/recovery artifact families, the
root-to-parent no-follow directory-descriptor binding, the strict
regular/single-link identity+digest proofs, the exclusive
temporary/recovery copy, replace/cleanup filesystem primitives, and the
durable artifact-handoff value codecs.  It never imports
``tm_migration``, ``tm_snapshot_recovery`` or ``tm_sqlite_store``:
owner state machines keep their decisions. Owner-private fault-injection
seams and error factories reach this module through explicit callbacks or
values supplied by late-bound wrappers; shared activation file identities
and path-safety primitives remain a direct Layer-1 dependency.

Authority retained by the owners:

* ``tm_migration`` keeps ``TMMigrationService`` export/refresh
  orchestration, canonical snapshot consumption, receipt/manifest
  construction and report/failure construction.
* ``tm_snapshot_recovery`` keeps receipt classification, terminal
  replay, divergence decisions and the recovery state machine.
* ``tm_sqlite_store`` keeps ledger/binding SQL, transaction
  boundaries, generation and coordinator/store authority.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from tm_activation_journal import (
    ActivationPreparationError,
    _ActivationFileIdentity,
    _activation_journal_path,
    _activation_journal_temp_path,
    _activation_lineage_marker_path,
    _activation_lineage_marker_temp_path,
    _activation_terminal_path,
    _activation_terminal_temp_path,
    _capture_activation_file,
    _remove_owned_activation_journal_temp,
)
from tm_contracts import (
    SNAPSHOT_MANIFEST_VERSION,
    CanonicalResourceIdentity,
    SnapshotKind,
    SnapshotManifest,
    SnapshotReceipt,
    contract_to_json,
    snapshot_receipt_digest,
)


_NATIVE_PATH_TYPE = type(Path())


_EXPORT_MANIFEST_SUFFIX = ".localcat-snapshot.json"
_EXPORT_JSONL_TEMP_SUFFIX = ".localcat-export.jsonl.tmp"
_EXPORT_MANIFEST_TEMP_SUFFIX = ".localcat-export.manifest.tmp"
_EXPORT_JSONL_RECOVERY_SUFFIX = ".localcat-export-recovery.jsonl.bak"
_EXPORT_MANIFEST_RECOVERY_SUFFIX = ".localcat-export-recovery.manifest.bak"


@dataclass(frozen=True)
class _CreatedFileIdentity:
    device: int
    inode: int


class _ExportParentHandle:
    """Resource-bound no-follow parent directory handle for one publication.

    Binding validates the full real non-symlink writable/executable
    parent chain and retains an ``O_DIRECTORY|O_NOFOLLOW`` descriptor of
    the exact immediate parent together with its device/inode identity.
    Every deterministic artifact operation of the publication protocol
    (create, open, verify, copy, replace, restore, cleanup) resolves
    basenames relative to this retained descriptor, so a hostile parent
    rename or replacement between phases can never redirect a
    destructive operation to another directory.  The descriptor is
    fsynced for publication/cleanup durability and the advertised full
    parent pathname is re-proven to still resolve to the retained
    identity at the required boundaries; any mismatch fails closed.
    The raw descriptor is private: contracts/specs never expose it.
    """

    __slots__ = ("destination", "_descriptor", "identity", "_closed")

    def __init__(
        self,
        destination: Path,
        descriptor: int,
        identity: tuple[int, int],
    ) -> None:
        if type(destination) is not _NATIVE_PATH_TYPE:
            raise TypeError("destination must be an exact native Path")
        if type(descriptor) is not int or descriptor < 0:
            raise TypeError("parent descriptor must be a non-negative int")
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or type(identity[0]) is not int
            or type(identity[1]) is not int
        ):
            raise TypeError("parent identity must be a device/inode pair")
        self.destination = destination
        self._descriptor = descriptor
        self.identity = identity
        self._closed = False

    @classmethod
    def bind(
        cls,
        destination: Path,
        *,
        after_chain_validated: Callable[[Path], None] | None = None,
    ) -> _ExportParentHandle:
        """Validate the full parent chain and retain one no-follow dirfd.

        The full chain is validated first, then the immediate parent is
        bound component-by-component from the filesystem root with
        ``O_DIRECTORY|O_NOFOLLOW`` for every component (one retained
        descriptor per level), so an ancestor swapped to a symlink
        between the initial validation and the bind can never redirect
        the retained descriptor into another tree: the walk fails closed
        on any missing, symlinked or non-directory component, and on
        platforms without component-safe binding (no ``O_DIRECTORY`` /
        ``O_NOFOLLOW`` or no ``dir_fd`` support).  The final descriptor
        must be a real writable/executable directory; its exact
        device/inode becomes the retained identity.
        """

        _require_export_parent_safe(destination)
        if after_chain_validated is not None:
            after_chain_validated(destination)
        if not (
            hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
        ):
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE")
        parent = destination.parent
        if not parent.is_absolute():
            raise ExportPreflightError("EXPORT.PATH_INVALID")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = -1
        try:
            descriptor = os.open(parent.parts[0], flags)
            for part in parent.parts[1:]:
                next_descriptor = os.open(
                    part,
                    flags,
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = next_descriptor
            observed = os.fstat(descriptor)
            if not stat.S_ISDIR(observed.st_mode):
                raise ExportPreflightError("EXPORT.PARENT_UNSAFE")
            if not (
                observed.st_mode
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                and observed.st_mode
                & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            ):
                raise ExportPreflightError("EXPORT.PARENT_UNSAFE")
            return cls(
                destination,
                descriptor,
                (observed.st_dev, observed.st_ino),
            )
        except ExportPreflightError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except (OSError, TypeError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE") from None

    @property
    def descriptor(self) -> int:
        """Private descriptor access for dirfd-relative syscalls."""

        if self._closed:
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE")
        return self._descriptor

    def fsync(self) -> None:
        """Fsync the retained directory descriptor for durability."""

        try:
            os.fsync(self.descriptor)
        except OSError as error:
            raise OSError("export parent directory fsync failed") from error

    def reprove(self) -> None:
        """Re-prove the advertised full parent pathname still resolves
        to the retained identity; a mismatch fails closed.

        The advertised pathname is re-walked component-by-component
        with ``O_DIRECTORY|O_NOFOLLOW`` per component, so an ancestor
        symlink can never be accepted as the proven chain; only the
        exact retained device/inode passes.
        """

        _require_export_parent_safe(self.destination)
        descriptor = -1
        try:
            descriptor = _open_export_parent_chain_no_follow(
                self.destination
            )
            observed = os.fstat(descriptor)
        except (OSError, TypeError, ValueError):
            raise ExportPreflightError(
                "EXPORT.PARENT_REPLACED"
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (observed.st_dev, observed.st_ino) != self.identity:
            raise ExportPreflightError("EXPORT.PARENT_REPLACED")

    def open(
        self,
        name: str,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        """Open one basename relative to the retained parent descriptor."""

        _require_export_basename(name)
        try:
            return os.open(
                name,
                flags,
                dir_fd=self.descriptor,
                mode=mode,
            )
        except OSError:
            raise

    def lstat(self, name: str) -> os.stat_result:
        """No-follow stat of one basename relative to the retained fd."""

        _require_export_basename(name)
        try:
            return os.stat(
                name,
                dir_fd=self.descriptor,
                follow_symlinks=False,
            )
        except OSError:
            raise

    def unlink(self, name: str) -> None:
        """Unlink one basename relative to the retained parent descriptor."""

        _require_export_basename(name)
        try:
            os.unlink(name, dir_fd=self.descriptor)
        except OSError:
            raise

    def replace(self, source: str, destination: str) -> None:
        """Rename one basename to another relative to the retained fd."""

        _require_export_basename(source)
        _require_export_basename(destination)
        try:
            os.replace(
                source,
                destination,
                src_dir_fd=self.descriptor,
                dst_dir_fd=self.descriptor,
            )
        except OSError:
            raise

    def close(self) -> None:
        """Best-effort close of the retained descriptor."""

        if self._closed:
            return
        self._closed = True
        descriptor = self._descriptor
        try:
            os.close(descriptor)
        except OSError:
            pass

    def __enter__(self) -> _ExportParentHandle:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()


def _require_export_basename(name: str) -> None:
    """One deterministic artifact name must be a pure safe basename."""

    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise ExportPreflightError("EXPORT.PATH_INVALID")


class _NoDestinationProof:
    """Sentinel for internal restore moves that do not publish a new file."""


_NO_DESTINATION_PROOF = _NoDestinationProof()


class ExportPreflightError(RuntimeError):
    """Stable export failure that never includes TM text or local paths."""

    def __init__(self, error_code: str) -> None:
        if type(error_code) is not str:
            raise TypeError("error_code must be a built-in string")
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class _ExportArtifactPaths:
    """Deterministic same-directory artifact family for one destination."""

    destination: Path
    manifest: Path
    jsonl_temp: Path
    manifest_temp: Path
    jsonl_recovery: Path
    manifest_recovery: Path

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.destination, "destination"),
            (self.manifest, "manifest"),
            (self.jsonl_temp, "jsonl_temp"),
            (self.manifest_temp, "manifest_temp"),
            (self.jsonl_recovery, "jsonl_recovery"),
            (self.manifest_recovery, "manifest_recovery"),
        ):
            if type(value) is not _NATIVE_PATH_TYPE:
                raise TypeError(f"{field_name} must be an exact native Path")
        if self.manifest != self.destination.with_name(
            f"{self.destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ):
            raise ValueError("export manifest path is not deterministic")


def _export_artifact_paths(destination: Path) -> _ExportArtifactPaths:
    if type(destination) is not _NATIVE_PATH_TYPE:
        raise TypeError("destination must be an exact native Path")
    return _ExportArtifactPaths(
        destination=destination,
        manifest=destination.with_name(
            f"{destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ),
        jsonl_temp=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_TEMP_SUFFIX}"
        ),
        manifest_temp=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_TEMP_SUFFIX}"
        ),
        jsonl_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_RECOVERY_SUFFIX}"
        ),
        manifest_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_RECOVERY_SUFFIX}"
        ),
    )


def _export_authority_paths(
    identity: CanonicalResourceIdentity,
) -> frozenset[Path]:
    """Deterministic authority paths this export must never touch.

    Covers the configured pair, sidecar, activation journal/marker/
    terminal families (final and temporary), and the deterministic
    temp/recovery artifact family of the configured pair itself, so an
    export destination can never reserve or collide with the paths the
    refresh/recovery protocol owns.
    """

    journal = _activation_journal_path(identity)
    marker = _activation_lineage_marker_path(identity)
    terminal = _activation_terminal_path(identity)
    configured_artifacts = _export_artifact_paths(
        identity.configured_jsonl_path
    )
    return frozenset(
        {
            identity.configured_jsonl_path,
            identity.snapshot_manifest_path,
            identity.canonical_sidecar_path,
            journal,
            _activation_journal_temp_path(journal),
            marker,
            _activation_lineage_marker_temp_path(marker),
            terminal,
            _activation_terminal_temp_path(terminal),
            configured_artifacts.jsonl_temp,
            configured_artifacts.manifest_temp,
            configured_artifacts.jsonl_recovery,
            configured_artifacts.manifest_recovery,
        }
    )


def _export_path_in_authority_family(
    identity: CanonicalResourceIdentity,
    path: Path,
) -> bool:
    """True when ``path`` is a deterministic artifact of this resource.

    The sidecar directory holds the activation journal/lineage/recovery
    and schema-upgrade artifact families plus every deterministic stage
    file; all of them embed either the sidecar name or the canonical
    target identity fragment.  A destination that collides with any of
    those names in the same directory is an authority-path alias.
    """

    sidecar = identity.canonical_sidecar_path
    if path.parent != sidecar.parent:
        return False
    name = path.name
    if name.startswith(f".{sidecar.name}.localcat-"):
        return True
    return name.startswith(".localcat-") and (
        identity.target_identity[:16] in name
    )


def _artifact_parent_identity(destination: Path) -> tuple[int, int]:
    """Exact immediate-parent device/inode under one strict chain proof.

    Runs the full real non-symlink writable/executable parent-chain
    proof and returns the immediate parent's exact device/inode so the
    issued registration can durably record it in the artifact handoff
    journal; terminal cleanup and handoff release re-prove it later.
    """

    _require_export_parent_safe(destination)
    try:
        observed = os.lstat(destination.parent)
    except (OSError, ValueError):
        raise ExportPreflightError("EXPORT.PARENT_UNSAFE") from None
    return (observed.st_dev, observed.st_ino)


def _open_export_parent_chain_no_follow(destination: Path) -> int:
    """Open ``destination.parent`` component-by-component from the root.

    Every pathname component from the filesystem root down to the
    immediate parent is opened with ``O_DIRECTORY|O_NOFOLLOW`` against
    the previously retained descriptor, so a symlinked, missing or
    non-directory component (including an ancestor swapped to a symlink
    between an earlier validation and this walk) fails the open instead
    of redirecting the descriptor into another tree.  The caller owns
    the returned descriptor.  Only usable where ``dir_fd``-relative
    opens are available; the caller maps any unsupported-platform
    failure to its fail-closed code.
    """

    parent = destination.parent
    if not parent.is_absolute():
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(parent.parts[0], flags)
    try:
        for part in parent.parts[1:]:
            next_descriptor = os.open(
                part,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _after_export_parent_chain_validated(destination: Path) -> None:
    """Late-bound seam invoked after the parent-chain validation.

    Test-only fault-injection point: runs immediately after the full
    real parent chain has been validated and before the component-wise
    no-follow descriptor binding, so an ancestor symlink swap exactly at
    that boundary can be reproduced without re-resolving the parent
    pathname for destructive work.
    """


def _after_replace_source_proved(
    source: Path,
    destination: Path,
    expected_source_identity: tuple[int, int],
) -> None:
    """Late-bound seam invoked immediately before the publication rename.

    Runs after the exact source identity proof and immediately before
    the mutation, so a source-name swap exactly at that boundary can be
    reproduced deterministically without re-resolving the source or
    destination pathname for destructive work.  Test-only
    fault-injection point.
    """


def _prove_replace_source(
    source: Path,
    *,
    expected_source_identity: tuple[int, int],
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Prove the exact single-link source identity for one publication.

    Resolves the basename relative to the retained parent descriptor
    when one is held, otherwise by strict no-follow pathname lstat; any
    observation other than the exact expected regular single-link inode
    fails closed with ``EXPORT.SOURCE_UNPROVEN`` before any mutation.
    """

    if parent_handle is not None:
        observed = parent_handle.lstat(source.name)
    else:
        observed = os.lstat(source)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino)
        != expected_source_identity
    ):
        raise ExportPreflightError("EXPORT.SOURCE_UNPROVEN")


def _prove_replace_destination(
    destination: Path,
    *,
    expected_destination_digest: str | None | _NoDestinationProof,
    expected_destination_identity: (
        tuple[int, int] | None | _NoDestinationProof
    ),
    parent_handle: _ExportParentHandle | None = None,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ] | None = None,
) -> None:
    """Prove the exact prior destination digest+identity or absence.

    Both publication-proof parameters must be set or both unset; a
    partial proof fails with ``ValueError`` before any probe.

    The destination must still match the exact prior proof recorded
    before publication: either the exact prior regular single-link
    digest AND inode, or the recorded exact absence.  Any divergence --
    including a same-byte foreign inode swapped in after an earlier
    proof -- fails closed with ``EXPORT.PRIOR_PAIR_CHANGED`` before any
    mutation, so a concurrent destination replacement in the late seam
    can never be silently overwritten by the rename.
    """

    if isinstance(
        expected_destination_digest,
        _NoDestinationProof,
    ) or isinstance(
        expected_destination_identity,
        _NoDestinationProof,
    ):
        raise ValueError("destination publication proof is incomplete")
    if parent_handle is not None:
        state, observed_digest, observed_identity = _dirfd_entry_state(
            destination,
            parent_handle=parent_handle,
        )
        if expected_destination_digest is None:
            if state != "absent":
                raise ExportPreflightError(
                    "EXPORT.PRIOR_PAIR_CHANGED"
                )
        elif (
            state != "present"
            or observed_digest != expected_destination_digest
            or observed_identity
            != expected_destination_identity
        ):
            raise ExportPreflightError(
                "EXPORT.PRIOR_PAIR_CHANGED"
            )
    else:
        assert strict_locator_proof is not None
        observed = _export_existing_state(
            destination,
            unsafe_code="EXPORT.PRIOR_PAIR_CHANGED",
            strict_locator_proof=strict_locator_proof,
        )
        expected = (
            None
            if expected_destination_digest is None
            else (
                expected_destination_digest,
                expected_destination_identity,
            )
        )
        if observed != expected:
            raise ExportPreflightError("EXPORT.PRIOR_PAIR_CHANGED")


def _require_export_parent_safe(destination: Path) -> None:
    """Fail closed on missing, symlinked, or unwritable parent chains."""

    parent = destination.parent
    if parent == destination:
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    chain = [parent]
    chain.extend(parent.parents)
    for candidate in reversed(chain):
        try:
            observed = os.lstat(candidate)
        except (OSError, ValueError):
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE") from None
        if not stat.S_ISDIR(observed.st_mode):
            raise ExportPreflightError("EXPORT.PARENT_UNSAFE")
    try:
        observed = os.lstat(parent)
    except (OSError, ValueError):
        raise ExportPreflightError("EXPORT.PARENT_UNSAFE") from None
    if not (
        observed.st_mode
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        and observed.st_mode
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        raise ExportPreflightError("EXPORT.PARENT_UNSAFE")


def _export_existing_digest(
    path: Path,
    *,
    unsafe_code: str,
) -> str | None:
    """Digest of an existing regular single-link file, or None when absent."""

    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ExportPreflightError(unsafe_code) from None
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise ExportPreflightError(unsafe_code)
    digest = _try_file_digest(path)
    if digest is None:
        raise ExportPreflightError(unsafe_code)
    return digest


def _export_existing_state(
    path: Path,
    *,
    unsafe_code: str,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ],
) -> tuple[str, tuple[int, int]] | None:
    """Strict digest and inode proof for an existing export entry."""

    digest = _export_existing_digest(path, unsafe_code=unsafe_code)
    if digest is None:
        return None
    proof = strict_locator_proof(path, digest)
    if proof is None:
        raise ExportPreflightError(unsafe_code)
    return (digest, proof[0])


def _validate_export_destination(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ],
) -> tuple[
    str | None,
    str | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    """Fail closed on any alias, unsafe parent, or conflicting path state.

    Returns the exact prior digests of the destination and its adjacent
    manifest (``None`` when the file is absent) so the caller can prove
    preservation and restore on failure.
    """

    destination = paths.destination
    if not destination.is_absolute():
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    if ".." in destination.parts or destination.name in {"", ".", ".."}:
        raise ExportPreflightError("EXPORT.PATH_INVALID")
    authority = _export_authority_paths(identity)
    for candidate in (destination, paths.manifest):
        if candidate in authority:
            raise ExportPreflightError("EXPORT.PATH_ALIASED")
        if _export_path_in_authority_family(identity, candidate):
            raise ExportPreflightError("EXPORT.PATH_ALIASED")
    if destination == paths.manifest:
        raise ExportPreflightError("EXPORT.PATH_ALIASED")
    for artifact in (
        paths.jsonl_temp,
        paths.manifest_temp,
        paths.jsonl_recovery,
        paths.manifest_recovery,
    ):
        if artifact in authority:
            raise ExportPreflightError("EXPORT.PATH_ALIASED")
    _require_export_parent_safe(destination)
    destination_state = _export_existing_state(
        paths.destination,
        unsafe_code="EXPORT.DESTINATION_UNSAFE",
        strict_locator_proof=strict_locator_proof,
    )
    manifest_state = _export_existing_state(
        paths.manifest,
        unsafe_code="EXPORT.MANIFEST_UNSAFE",
        strict_locator_proof=strict_locator_proof,
    )
    destination_before = (
        None if destination_state is None else destination_state[0]
    )
    destination_identity = (
        None if destination_state is None else destination_state[1]
    )
    manifest_before = None if manifest_state is None else manifest_state[0]
    manifest_identity = None if manifest_state is None else manifest_state[1]
    if destination_before is None and manifest_before is not None:
        raise ExportPreflightError("EXPORT.PAIR_INCONSISTENT")
    for artifact, code in (
        (paths.jsonl_temp, "EXPORT.TEMP_CONFLICT"),
        (paths.manifest_temp, "EXPORT.TEMP_CONFLICT"),
        (paths.jsonl_recovery, "EXPORT.RECOVERY_CONFLICT"),
        (paths.manifest_recovery, "EXPORT.RECOVERY_CONFLICT"),
    ):
        if _path_exists(artifact):
            raise ExportPreflightError(code)
    return (
        destination_before,
        manifest_before,
        destination_identity,
        manifest_identity,
    )


def _refresh_destination_state(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ],
) -> tuple[
    str | None,
    str | None,
    tuple[int, int] | None,
    tuple[int, int] | None,
]:
    """Capture the provable prior state of the configured snapshot pair.

    Task 5.13 configured refresh: the published pair is always the
    service resource identity's configured JSONL and its deterministic
    adjacent manifest; callers cannot supply another path.  The prior
    entries are captured independently as regular single-link files or
    absence, and the parent chain must be safe.  Pair consistency is
    intentionally decided by ``SourceBindingMonitor.observe()`` so a
    missing/asymmetric pair durably latches ``SOURCE_DIVERGED``.
    """

    if paths.destination != identity.configured_jsonl_path:
        raise ExportPreflightError("REFRESH.PATH_NOT_CONFIGURED")
    if paths.manifest != identity.snapshot_manifest_path:
        raise ExportPreflightError("REFRESH.PATH_NOT_CONFIGURED")
    destination_state = _export_existing_state(
        paths.destination,
        unsafe_code="REFRESH.CONFIGURED_UNSAFE",
        strict_locator_proof=strict_locator_proof,
    )
    manifest_state = _export_existing_state(
        paths.manifest,
        unsafe_code="REFRESH.MANIFEST_UNSAFE",
        strict_locator_proof=strict_locator_proof,
    )
    destination_before = (
        None if destination_state is None else destination_state[0]
    )
    destination_identity = (
        None if destination_state is None else destination_state[1]
    )
    manifest_before = None if manifest_state is None else manifest_state[0]
    manifest_identity = None if manifest_state is None else manifest_state[1]
    _require_export_parent_safe(paths.destination)
    return (
        destination_before,
        manifest_before,
        destination_identity,
        manifest_identity,
    )


def _require_refresh_artifacts_absent(
    identity: CanonicalResourceIdentity,
    paths: _ExportArtifactPaths,
) -> None:
    """Fail closed on any conflicting same-directory refresh artifact.

    The refresh artifacts are themselves the deterministic configured
    artifact family, so they are excluded from the authority set they
    legitimately occupy; the check still rejects a collision with the
    journal/marker/terminal or any other authority path.
    """

    configured = _export_artifact_paths(identity.configured_jsonl_path)
    own_artifacts = frozenset(
        {
            configured.jsonl_temp,
            configured.manifest_temp,
            configured.jsonl_recovery,
            configured.manifest_recovery,
        }
    )
    for artifact in (
        paths.jsonl_temp,
        paths.manifest_temp,
        paths.jsonl_recovery,
        paths.manifest_recovery,
    ):
        if artifact in _export_authority_paths(identity) - own_artifacts:
            raise ExportPreflightError("REFRESH.PATH_ALIASED")
    for artifact, code in (
        (paths.jsonl_temp, "REFRESH.TEMP_CONFLICT"),
        (paths.manifest_temp, "REFRESH.TEMP_CONFLICT"),
        (paths.jsonl_recovery, "REFRESH.RECOVERY_CONFLICT"),
        (paths.manifest_recovery, "REFRESH.RECOVERY_CONFLICT"),
    ):
        if _path_exists(artifact):
            raise ExportPreflightError(code)


def _path_exists(
    path: Path,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """True when one deterministic entry exists under the retained parent
    descriptor, or by pathname when no handle is supplied."""

    if parent_handle is not None:
        try:
            parent_handle.lstat(path.name)
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError):
            raise ExportPreflightError("EXPORT.PATH_UNREADABLE") from None
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise ExportPreflightError("EXPORT.PATH_UNREADABLE") from None


def _fsync_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _fsync_directory(
    path: Path,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Fsync one directory for publication/cleanup durability.

    With a retained parent handle the fsync runs on the exact retained
    descriptor and the advertised full parent pathname is then re-proven
    to still resolve to the same identity; a mismatch fails closed with
    ``EXPORT.PARENT_REPLACED``.  Without a handle the directory is
    opened by pathname exactly as before.
    """

    if parent_handle is not None:
        parent_handle.fsync()
        parent_handle.reprove()
        return
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _created_export_identity(
    descriptor: int,
) -> tuple[_CreatedFileIdentity, OSError | None]:
    """Read a creation handle identity, retaining one transient failure.

    A second read through the still-open handle establishes safe unlink
    authority without trusting the pathname.  The caller then reports the
    first failure after cleaning the inode it can now prove it created.
    """

    first_error: OSError | None = None
    try:
        observed = os.fstat(descriptor)
    except OSError as error:
        first_error = error
        observed = os.fstat(descriptor)
    return (
        _CreatedFileIdentity(observed.st_dev, observed.st_ino),
        first_error,
    )


def _replace_path(
    source: Path,
    destination: Path,
    *,
    expected_source_identity: tuple[int, int],
    expected_destination_digest: str | None | _NoDestinationProof = (
        _NO_DESTINATION_PROOF
    ),
    expected_destination_identity: (
        tuple[int, int] | None | _NoDestinationProof
    ) = _NO_DESTINATION_PROOF,
    parent_handle: _ExportParentHandle | None = None,
    after_source_proved: Callable[[Path, Path, tuple[int, int]], None] | None = None,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ] | None = None,
) -> None:
    if (
        type(expected_source_identity) is not tuple
        or len(expected_source_identity) != 2
        or type(expected_source_identity[0]) is not int
        or type(expected_source_identity[1]) is not int
    ):
        raise ValueError("expected source identity is invalid")
    digest_unset = isinstance(
        expected_destination_digest,
        _NoDestinationProof,
    )
    identity_unset = isinstance(
        expected_destination_identity,
        _NoDestinationProof,
    )
    if digest_unset != identity_unset:
        raise ValueError("destination publication proof is incomplete")
    if not digest_unset:
        _prove_replace_destination(
            destination,
            expected_destination_digest=expected_destination_digest,
            expected_destination_identity=expected_destination_identity,
            parent_handle=parent_handle,
            strict_locator_proof=strict_locator_proof,
        )
    _prove_replace_source(
        source,
        expected_source_identity=expected_source_identity,
        parent_handle=parent_handle,
    )
    if after_source_proved is not None:
        after_source_proved(
            source,
            destination,
            expected_source_identity,
        )
    _prove_replace_source(
        source,
        expected_source_identity=expected_source_identity,
        parent_handle=parent_handle,
    )
    if not digest_unset:
        _prove_replace_destination(
            destination,
            expected_destination_digest=expected_destination_digest,
            expected_destination_identity=expected_destination_identity,
            parent_handle=parent_handle,
            strict_locator_proof=strict_locator_proof,
        )
    if parent_handle is not None:
        parent_handle.replace(source.name, destination.name)
    else:
        os.replace(source, destination)
    if parent_handle is not None:
        observed = parent_handle.lstat(destination.name)
    else:
        observed = os.lstat(destination)
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino)
        != expected_source_identity
    ):
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")


def _stream_export_jsonl_temp(
    path: Path,
    records: tuple[object, ...],
    *,
    parent_handle: _ExportParentHandle | None = None,
    fsync_file: Callable[[int], None] | None = None,
    export_jsonl_row: Callable[[object], dict[str, object]],
) -> tuple[str, int, _CreatedFileIdentity]:
    """Stream one exclusive JSONL temporary file and fsync it.

    With a retained parent handle the exclusive no-follow create runs
    relative to the retained descriptor; cleanup on failure is likewise
    descriptor-relative.
    """

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if parent_handle is not None:
            descriptor = parent_handle.open(path.name, flags, 0o600)
        else:
            descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_CONFLICT") from error
    identity: _CreatedFileIdentity | None = None
    try:
        identity, identity_error = _created_export_identity(descriptor)
        if identity_error is not None:
            raise ExportPreflightError(
                "EXPORT.TEMP_IDENTITY_FAILED"
            ) from identity_error
        os.fchmod(descriptor, 0o600)
        digest = hashlib.sha256()
        count = 0
        for item in records:
            payload = (
                json.dumps(
                    export_jsonl_row(item),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            digest.update(payload)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("export write made no progress")
                view = view[written:]
            count += 1
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExportPreflightError("EXPORT.TEMP_UNSAFE")
        try:
            (fsync_file or _fsync_file)(descriptor)
        except OSError as error:
            raise ExportPreflightError(
                "EXPORT.JSONL_FSYNC_FAILED"
            ) from error
        return (
            digest.hexdigest(),
            count,
            identity,
        )
    except ExportPreflightError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
            parent_handle=parent_handle,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise
    except OSError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
            parent_handle=parent_handle,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise ExportPreflightError("EXPORT.JSONL_WRITE_FAILED") from error
    finally:
        os.close(descriptor)


def _verify_export_jsonl_temp(
    path: Path,
    *,
    expected_digest: str,
    expected_count: int,
    identity: _CreatedFileIdentity,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Re-open and re-validate one JSONL temporary before publication."""

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    try:
        if parent_handle is not None:
            descriptor = parent_handle.open(
                path.name,
                os.O_RDONLY | no_follow,
            )
        else:
            descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_UNREADABLE") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            count += chunk.count(b"\n")
        if (
            digest.hexdigest() != expected_digest
            or count != expected_count
        ):
            raise ExportPreflightError("EXPORT.JSONL_VERIFY_FAILED")
        if parent_handle is not None:
            final = parent_handle.lstat(path.name)
        else:
            final = os.lstat(path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
    finally:
        os.close(descriptor)


def _write_export_payload_temp(
    path: Path,
    payload: bytes,
    *,
    parent_handle: _ExportParentHandle | None = None,
    fsync_file: Callable[[int], None] | None = None,
) -> _CreatedFileIdentity:
    """Write one exclusive manifest temporary file and fsync it.

    With a retained parent handle the exclusive no-follow create runs
    relative to the retained descriptor; cleanup on failure is likewise
    descriptor-relative.
    """

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if parent_handle is not None:
            descriptor = parent_handle.open(path.name, flags, 0o600)
        else:
            descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_CONFLICT") from error
    identity: _CreatedFileIdentity | None = None
    try:
        identity, identity_error = _created_export_identity(descriptor)
        if identity_error is not None:
            raise ExportPreflightError(
                "EXPORT.TEMP_IDENTITY_FAILED"
            ) from identity_error
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("export write made no progress")
            view = view[written:]
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExportPreflightError("EXPORT.TEMP_UNSAFE")
        try:
            (fsync_file or _fsync_file)(descriptor)
        except OSError as error:
            raise ExportPreflightError(
                "EXPORT.MANIFEST_FSYNC_FAILED"
            ) from error
        return identity
    except ExportPreflightError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
            parent_handle=parent_handle,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise
    except OSError as error:
        if identity is None or not _remove_failed_export_artifact(
            path,
            identity,
            parent_handle=parent_handle,
        ):
            raise ExportPreflightError(
                "EXPORT.TEMP_CLEANUP_FAILED"
            ) from error
        raise ExportPreflightError(
            "EXPORT.MANIFEST_WRITE_FAILED"
        ) from error
    finally:
        os.close(descriptor)


def _verify_export_payload_temp(
    path: Path,
    *,
    expected_bytes: bytes,
    identity: _CreatedFileIdentity,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Re-open and re-validate one manifest temporary before publication."""

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    try:
        if parent_handle is not None:
            descriptor = parent_handle.open(
                path.name,
                os.O_RDONLY | no_follow,
            )
        else:
            descriptor = os.open(path, os.O_RDONLY | no_follow)
    except OSError as error:
        raise ExportPreflightError("EXPORT.TEMP_UNREADABLE") from error
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
            or (observed.st_dev, observed.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if b"".join(chunks) != expected_bytes:
            raise ExportPreflightError("EXPORT.MANIFEST_VERIFY_FAILED")
        if parent_handle is not None:
            final = parent_handle.lstat(path.name)
        else:
            final = os.lstat(path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino)
            != (identity.device, identity.inode)
        ):
            raise ExportPreflightError("EXPORT.TEMP_SWAPPED")
    finally:
        os.close(descriptor)


def _remove_exported_if_owned(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
    expected_digest: str | None,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Unlink one published file only when its exact identity still holds.

    Returns True when the path was never published by us (nothing to
    restore), is already absent, or was removed by us.  A foreign swap
    (different inode or digest) fails closed without deleting anything.
    With a retained parent handle the proof and unlink are
    descriptor-relative.
    """

    if expected_identity is None or expected_digest is None:
        return not _path_exists(path, parent_handle=parent_handle)
    proof = _strict_regular_file_state(
        path,
        parent_handle=parent_handle,
    )
    if (
        proof is None
        or proof[0] != expected_digest
        or proof[1] != expected_identity
    ):
        return False
    try:
        if parent_handle is not None:
            observed = parent_handle.lstat(path.name)
        else:
            observed = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        not stat.S_ISREG(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != expected_identity
    ):
        return False
    try:
        if parent_handle is not None:
            parent_handle.unlink(path.name)
        else:
            path.unlink()
    except OSError:
        return False
    return not _path_exists(path, parent_handle=parent_handle)


def _copy_export_prior_pair(
    paths: _ExportArtifactPaths,
    *,
    destination_before: str | None,
    manifest_before: str | None,
    parent_handle: _ExportParentHandle | None = None,
    copy_recovery_file: Callable[..., _CreatedFileIdentity] | None = None,
) -> tuple[
    _CreatedFileIdentity | None,
    _CreatedFileIdentity | None,
]:
    """Stream-copy the exact prior pair into exclusive recovery files.

    Returns the (jsonl, manifest) recovery identities.  A digest mismatch
    between validation and copy means the prior pair changed under us;
    that fails stop before any publication side effect.  With a retained
    parent handle the exclusive recovery creates and the prior source
    reads are descriptor-relative.
    """

    jsonl_identity: _CreatedFileIdentity | None = None
    manifest_identity: _CreatedFileIdentity | None = None
    if destination_before is not None:
        jsonl_identity = (copy_recovery_file or _copy_export_recovery_file)(
            paths.destination,
            paths.jsonl_recovery,
            expected_digest=destination_before,
            code="EXPORT.JSONL_RECOVERY_COPY_FAILED",
            parent_handle=parent_handle,
        )
    if manifest_before is not None:
        try:
            manifest_identity = (copy_recovery_file or _copy_export_recovery_file)(
                paths.manifest,
                paths.manifest_recovery,
                expected_digest=manifest_before,
                code="EXPORT.MANIFEST_RECOVERY_COPY_FAILED",
                parent_handle=parent_handle,
            )
        except ExportPreflightError as error:
            if (
                jsonl_identity is not None
                and not _remove_failed_export_artifact(
                    paths.jsonl_recovery,
                    jsonl_identity,
                    parent_handle=parent_handle,
                )
            ):
                raise ExportPreflightError(
                    "EXPORT.RECOVERY_CLEANUP_FAILED"
                ) from error
            raise
    return jsonl_identity, manifest_identity


def _copy_export_recovery_file(
    source: Path,
    recovery: Path,
    *,
    expected_digest: str,
    code: str,
    parent_handle: _ExportParentHandle | None = None,
    fsync_file: Callable[[int], None] | None = None,
) -> _CreatedFileIdentity:
    """Copy one prior file into an owned exclusive recovery file."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if parent_handle is not None:
            descriptor = parent_handle.open(recovery.name, flags, 0o600)
        else:
            descriptor = os.open(recovery, flags, 0o600)
    except OSError as error:
        raise ExportPreflightError("EXPORT.RECOVERY_CONFLICT") from error
    identity: _CreatedFileIdentity | None = None
    source_descriptor = -1
    try:
        identity, identity_error = _created_export_identity(descriptor)
        if identity_error is not None:
            raise ExportPreflightError(
                "EXPORT.RECOVERY_IDENTITY_FAILED"
            ) from identity_error
        os.fchmod(descriptor, 0o600)
        no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
        if parent_handle is not None:
            source_descriptor = parent_handle.open(
                source.name,
                os.O_RDONLY | no_follow,
            )
        else:
            source_descriptor = os.open(source, os.O_RDONLY | no_follow)
        observed = os.fstat(source_descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_nlink != 1
        ):
            raise ExportPreflightError(code)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("export recovery copy made no progress")
                view = view[written:]
        if digest.hexdigest() != expected_digest:
            raise ExportPreflightError("EXPORT.PRIOR_PAIR_CHANGED")
        os.close(source_descriptor)
        source_descriptor = -1
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise ExportPreflightError("EXPORT.RECOVERY_UNSAFE")
        try:
            (fsync_file or _fsync_file)(descriptor)
        except OSError as error:
            raise ExportPreflightError(code) from error
        return identity
    except ExportPreflightError as error:
        if identity is None or not _remove_failed_export_artifact(
            recovery,
            identity,
            parent_handle=parent_handle,
        ):
            raise ExportPreflightError(
                "EXPORT.RECOVERY_CLEANUP_FAILED"
            ) from error
        raise
    except OSError as error:
        if identity is None or not _remove_failed_export_artifact(
            recovery,
            identity,
            parent_handle=parent_handle,
        ):
            raise ExportPreflightError(
                "EXPORT.RECOVERY_CLEANUP_FAILED"
            ) from error
        raise ExportPreflightError(code) from error
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        os.close(descriptor)


def _entry_is_owned(
    path: Path,
    *,
    identity: tuple[int, int] | None,
    digest: str | None,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Prove one published file by digest AND exact created inode."""

    if identity is None or digest is None:
        return False
    proof = _strict_regular_file_state(
        path,
        parent_handle=parent_handle,
    )
    return (
        proof is not None
        and proof[0] == digest
        and proof[1] == identity
    )


def _restore_export_pair(
    paths: _ExportArtifactPaths,
    *,
    destination_before: str | None,
    manifest_before: str | None,
    jsonl_published_identity: tuple[int, int] | None,
    manifest_published_identity: tuple[int, int] | None,
    jsonl_digest: str | None,
    manifest_digest: str | None,
    parent_handle: _ExportParentHandle | None = None,
    replace_path: Callable[..., None] | None = None,
    fsync_directory: Callable[..., None] | None = None,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ] | None = None,
) -> None:
    """Restore the exact prior pair or the original absence, then fsync.

    The destination is only replaced from the owned recovery copy when
    the current entry is still provably this export's publication; a
    foreign entry is left untouched and fails stop.  With a retained
    parent handle every proof, replace, unlink and the terminal
    directory fsync are descriptor-relative.
    """

    if destination_before is not None:
        current_destination = _strict_regular_file_state(
            paths.destination,
            parent_handle=parent_handle,
        )
        if (
            current_destination is None
            or current_destination[0] != destination_before
        ):
            if not _entry_is_owned(
                paths.destination,
                identity=jsonl_published_identity,
                digest=jsonl_digest,
                parent_handle=parent_handle,
            ):
                raise ExportPreflightError("EXPORT.JSONL_RESTORE_FAILED")
            assert jsonl_digest is not None
            assert jsonl_published_identity is not None
            _restore_export_from_recovery(
                paths.jsonl_recovery,
                paths.destination,
                expected_digest=destination_before,
                expected_destination_digest=jsonl_digest,
                expected_destination_identity=jsonl_published_identity,
                code="EXPORT.JSONL_RESTORE_FAILED",
                parent_handle=parent_handle,
                replace_path=replace_path,
                strict_locator_proof=strict_locator_proof,
            )
    elif not _remove_exported_if_owned(
        paths.destination,
        expected_identity=jsonl_published_identity,
        expected_digest=jsonl_digest,
        parent_handle=parent_handle,
    ):
        raise ExportPreflightError("EXPORT.JSONL_RESTORE_FAILED")
    if manifest_before is not None:
        current_manifest = _strict_regular_file_state(
            paths.manifest,
            parent_handle=parent_handle,
        )
        if (
            current_manifest is None
            or current_manifest[0] != manifest_before
        ):
            if not _entry_is_owned(
                paths.manifest,
                identity=manifest_published_identity,
                digest=manifest_digest,
                parent_handle=parent_handle,
            ):
                raise ExportPreflightError("EXPORT.MANIFEST_RESTORE_FAILED")
            assert manifest_digest is not None
            assert manifest_published_identity is not None
            _restore_export_from_recovery(
                paths.manifest_recovery,
                paths.manifest,
                expected_digest=manifest_before,
                expected_destination_digest=manifest_digest,
                expected_destination_identity=manifest_published_identity,
                code="EXPORT.MANIFEST_RESTORE_FAILED",
                parent_handle=parent_handle,
                replace_path=replace_path,
                strict_locator_proof=strict_locator_proof,
            )
    elif not _remove_exported_if_owned(
        paths.manifest,
        expected_identity=manifest_published_identity,
        expected_digest=manifest_digest,
        parent_handle=parent_handle,
    ):
        raise ExportPreflightError("EXPORT.MANIFEST_RESTORE_FAILED")
    try:
        (fsync_directory or _fsync_directory)(
            paths.destination.parent,
            parent_handle=parent_handle,
        )
    except OSError as error:
        raise ExportPreflightError(
            "EXPORT.RESTORE_FSYNC_FAILED"
        ) from error


def _restore_export_from_recovery(
    recovery: Path,
    destination: Path,
    *,
    expected_digest: str,
    expected_destination_digest: str,
    expected_destination_identity: tuple[int, int],
    code: str,
    parent_handle: _ExportParentHandle | None = None,
    replace_path: Callable[..., None] | None = None,
    strict_locator_proof: Callable[
        [Path, str], tuple[tuple[int, int], str] | None
    ] | None = None,
) -> None:
    """Replace one owned published file with the prior-bytes recovery copy.

    The recovery copy must still pass the strict no-follow, regular,
    single-link, digest and stable-identity proof; an unprovable or
    swapped recovery file fails stop and is never used to overwrite.
    With a retained parent handle the proof and the replace are
    descriptor-relative.
    """

    if parent_handle is not None:
        regular_proof = _strict_regular_file_state(
            recovery,
            parent_handle=parent_handle,
        )
        if (
            regular_proof is None
            or regular_proof[0] != expected_digest
        ):
            raise ExportPreflightError(code)
        source_identity = regular_proof[1]
    else:
        assert strict_locator_proof is not None
        locator_proof = strict_locator_proof(recovery, expected_digest)
        if locator_proof is None:
            raise ExportPreflightError(code)
        source_identity = locator_proof[0]
    try:
        (replace_path or _replace_path)(
            recovery,
            destination,
            expected_source_identity=source_identity,
            expected_destination_digest=expected_destination_digest,
            expected_destination_identity=expected_destination_identity,
            parent_handle=parent_handle,
            strict_locator_proof=strict_locator_proof,
        )
    except OSError as error:
        raise ExportPreflightError(code) from error


def _dirfd_entry_state(
    path: Path,
    *,
    parent_handle: _ExportParentHandle,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """One descriptor-relative three-way proof of an export entry.

    Returns ``("absent", None, None)`` when the basename does not exist
    under the retained parent descriptor, ``("unsafe", None, None)``
    when the entry is a symlink, directory, multi-link, unreadable or
    identity-unstable, and ``("present", digest, identity)`` only for a
    regular single-link file hashed through a no-follow descriptor and
    re-proven by terminal ``lstat`` relative to the same retained fd.
    """

    try:
        observed = parent_handle.lstat(path.name)
    except FileNotFoundError:
        return ("absent", None, None)
    except OSError:
        return ("unsafe", None, None)
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        return ("unsafe", None, None)
    identity = (observed.st_dev, observed.st_ino)
    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        descriptor = parent_handle.open(
            path.name,
            os.O_RDONLY | no_follow,
        )
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != identity
        ):
            return ("unsafe", None, None)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        return ("unsafe", None, None)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    try:
        final = parent_handle.lstat(path.name)
    except OSError:
        return ("unsafe", None, None)
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != identity
    ):
        return ("unsafe", None, None)
    return ("present", digest.hexdigest(), identity)


def _strict_regular_file_state(
    path: Path,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[str, tuple[int, int]] | None:
    """One descriptor-based no-follow proof of a published/owned entry.

    Returns ``(digest, identity)`` only when the path opens
    ``O_NOFOLLOW``, ``fstat`` reports a regular single-link file, its
    bytes hash to a stable digest read through the descriptor, and a
    terminal ``lstat`` still reports the same device/inode.  Absent,
    symlinked, directory, multi-link, swapped, or unreadable entries
    return ``None`` and are never hashed by pathname.  With a retained
    parent handle the open and the terminal lstat are
    descriptor-relative.
    """

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        if parent_handle is not None:
            descriptor = parent_handle.open(
                path.name,
                os.O_RDONLY | no_follow,
            )
        else:
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
        os.close(descriptor)
        descriptor = -1
        if parent_handle is not None:
            final = parent_handle.lstat(path.name)
        else:
            final = os.lstat(path)
        if (
            not stat.S_ISREG(final.st_mode)
            or final.st_nlink != 1
            or (final.st_dev, final.st_ino) != identity
        ):
            return None
        return digest.hexdigest(), identity
    except OSError:
        return None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _published_file_identity(
    path: Path,
    expected_digest: str,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[int, int]:
    """Prove the just-published file still holds our exact bytes."""

    proof = _strict_regular_file_state(
        path,
        parent_handle=parent_handle,
    )
    if proof is None or proof[0] != expected_digest:
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")
    return proof[1]


def _verify_export_pair(
    paths: _ExportArtifactPaths,
    *,
    jsonl_digest: str,
    manifest_bytes: bytes,
    jsonl_identity: tuple[int, int],
    manifest_identity: tuple[int, int],
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Re-prove the published pair by digest AND exact created identity.

    A foreign same-byte inode swap cannot pass: the strict
    descriptor-based proof must report the exact identities of the
    files this publication created.  With a retained parent handle the
    proofs are descriptor-relative.
    """

    jsonl_proof = _strict_regular_file_state(
        paths.destination,
        parent_handle=parent_handle,
    )
    if (
        jsonl_proof is None
        or jsonl_proof[0] != jsonl_digest
        or jsonl_proof[1] != jsonl_identity
    ):
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")
    manifest_proof = _strict_regular_file_state(
        paths.manifest,
        parent_handle=parent_handle,
    )
    if (
        manifest_proof is None
        or manifest_proof[0]
        != hashlib.sha256(manifest_bytes).hexdigest()
        or manifest_proof[1] != manifest_identity
    ):
        raise ExportPreflightError("EXPORT.PUBLISH_VERIFY_FAILED")


def _cleanup_export_artifacts(
    paths: _ExportArtifactPaths,
    *,
    jsonl_temp_identity: _CreatedFileIdentity | None,
    manifest_temp_identity: _CreatedFileIdentity | None,
    jsonl_recovery_identity: _CreatedFileIdentity | None,
    manifest_recovery_identity: _CreatedFileIdentity | None,
    parent_handle: _ExportParentHandle | None = None,
) -> tuple[Path, ...]:
    """Remove only artifacts whose creation identity we still own.

    Returns the paths that could not be removed (hostile swap or I/O
    failure); those are never deleted and fail the next export closed.
    The manifest recovery copy is processed before the JSONL recovery
    copy so a partial cleanup that returns ``remaining`` always leaves
    the JSONL recovery locator provable when a prior pair existed.
    With a retained parent handle the proof and unlink are
    descriptor-relative.
    """

    remaining: list[Path] = []
    for path, identity in (
        (paths.jsonl_temp, jsonl_temp_identity),
        (paths.manifest_temp, manifest_temp_identity),
        (paths.manifest_recovery, manifest_recovery_identity),
        (paths.jsonl_recovery, jsonl_recovery_identity),
    ):
        if identity is None:
            continue
        try:
            _remove_created_file(
                path,
                identity,
                parent_handle=parent_handle,
            )
            if _path_exists(path, parent_handle=parent_handle):
                remaining.append(path)
        except (OSError, ExportPreflightError):
            remaining.append(path)
    return tuple(remaining)


def _remove_failed_export_artifact(
    path: Path,
    identity: _CreatedFileIdentity,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> bool:
    """Remove an artifact created by this call after an ordinary exception.

    Process termination can still leave the deterministic temp for the
    cross-call recovery protocol, but a caught write/fsync failure must not
    turn an otherwise retryable export into a permanent TEMP_CONFLICT.
    With a retained parent handle the proof and unlink are
    descriptor-relative.
    """

    try:
        _remove_created_file(
            path,
            identity,
            parent_handle=parent_handle,
        )
        return not _path_exists(path, parent_handle=parent_handle)
    except (OSError, ExportPreflightError):
        return False


def _remove_created_file(
    path: Path,
    expected: _CreatedFileIdentity,
    *,
    parent_handle: _ExportParentHandle | None = None,
) -> None:
    """Unlink one identity-proven entry, by pathname or by retained
    parent descriptor."""

    try:
        if parent_handle is not None:
            observed = parent_handle.lstat(path.name)
        else:
            observed = os.lstat(path)
    except FileNotFoundError:
        return
    if (
        stat.S_ISREG(observed.st_mode)
        and observed.st_dev == expected.device
        and observed.st_ino == expected.inode
    ):
        if parent_handle is not None:
            parent_handle.unlink(path.name)
        else:
            path.unlink()


def _try_file_digest(path: Path) -> str | None:
    """Hash one file's exact bytes, or return None when unreadable."""

    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


class RecoveryError(RuntimeError):
    """Stable code-only recovery failure with no path or TM payload."""

    def __init__(self, error_code: str, *, retryable: bool) -> None:
        if type(error_code) is not str or not error_code:
            raise TypeError("recovery error code is invalid")
        if type(retryable) is not bool:
            raise TypeError("recovery retryable flag is invalid")
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


@dataclass(frozen=True)
class _ArtifactHandoffFacts:
    """One durable write-once ownership record for one issued receipt.

    The publisher records the exclusive temporary identities at
    registration, the recovery-copy identities after the copies are
    prepared, and the prior final JSONL/manifest identity+digest (or
    explicit proven absence) captured before any publication replace.
    Recovery treats a matching regular single-link file as owned only
    when its exact identity was durably handed off; content equality
    alone never proves ownership, and a prior record is either proven
    absent (``prior_*_absent`` True), proven present (digest+identity),
    or unrecorded (all three ``None``) which always fails closed.  The
    exact immediate-parent device/inode proven by a strict real
    non-symlink parent-chain proof at registration is durably recorded
    (``artifact_parent_identity``); a missing legacy identity always
    fails terminal replay closed and is never inferred.
    """

    snapshot_id: str
    jsonl_temp_identity: tuple[int, int] | None
    manifest_temp_identity: tuple[int, int] | None
    artifact_parent_identity: tuple[int, int] | None = None
    jsonl_recovery_identity: tuple[int, int] | None = None
    manifest_recovery_identity: tuple[int, int] | None = None
    prior_jsonl_identity: tuple[int, int] | None = None
    prior_jsonl_digest: str | None = None
    prior_jsonl_absent: bool | None = None
    prior_manifest_identity: tuple[int, int] | None = None
    prior_manifest_digest: str | None = None
    prior_manifest_absent: bool | None = None

    def __post_init__(self) -> None:
        if type(self.snapshot_id) is not str or not self.snapshot_id:
            raise TypeError("handoff snapshot id must be a non-empty string")
        for value, field_name in (
            (self.jsonl_temp_identity, "jsonl_temp_identity"),
            (self.manifest_temp_identity, "manifest_temp_identity"),
            (self.artifact_parent_identity, "artifact_parent_identity"),
            (self.jsonl_recovery_identity, "jsonl_recovery_identity"),
            (self.manifest_recovery_identity, "manifest_recovery_identity"),
        ):
            if value is not None and (
                type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not int
                or type(value[1]) is not int
            ):
                raise TypeError(
                    f"{field_name} must be a device/inode identity pair"
                )
        for value, field_name in (
            (self.prior_jsonl_identity, "prior_jsonl_identity"),
            (self.prior_manifest_identity, "prior_manifest_identity"),
        ):
            if value is not None and (
                type(value) is not tuple
                or len(value) != 2
                or type(value[0]) is not int
                or type(value[1]) is not int
            ):
                raise TypeError(
                    f"{field_name} must be a device/inode identity pair"
                )
        for digest, field_name in (
            (self.prior_jsonl_digest, "prior_jsonl_digest"),
            (self.prior_manifest_digest, "prior_manifest_digest"),
        ):
            if digest is not None and (
                type(digest) is not str
                or len(digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in digest
                )
            ):
                raise TypeError(
                    f"{field_name} must be a 64-hex digest string"
                )
        for absent, field_name in (
            (self.prior_jsonl_absent, "prior_jsonl_absent"),
            (self.prior_manifest_absent, "prior_manifest_absent"),
        ):
            if absent is not None and type(absent) is not bool:
                raise TypeError(f"{field_name} must be a bool or None")


@dataclass(frozen=True)
class _RecoveryArtifactPaths:
    """Deterministic same-directory artifact family (Task 5.12 frozen)."""

    destination: Path
    manifest: Path
    jsonl_temp: Path
    manifest_temp: Path
    jsonl_recovery: Path
    manifest_recovery: Path

    def __post_init__(self) -> None:
        for value in (
            self.destination,
            self.manifest,
            self.jsonl_temp,
            self.manifest_temp,
            self.jsonl_recovery,
            self.manifest_recovery,
        ):
            if type(value) is not type(Path()):
                raise TypeError("artifact paths must be exact Path values")
        if self.manifest != self.destination.with_name(
            f"{self.destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ):
            raise ValueError("manifest path is not deterministic")


def _recovery_artifact_paths(destination: Path) -> _RecoveryArtifactPaths:
    if type(destination) is not type(Path()):
        raise TypeError("destination must be an exact Path")
    return _RecoveryArtifactPaths(
        destination=destination,
        manifest=destination.with_name(
            f"{destination.name}{_EXPORT_MANIFEST_SUFFIX}"
        ),
        jsonl_temp=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_TEMP_SUFFIX}"
        ),
        manifest_temp=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_TEMP_SUFFIX}"
        ),
        jsonl_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_JSONL_RECOVERY_SUFFIX}"
        ),
        manifest_recovery=destination.with_name(
            f".{destination.name}{_EXPORT_MANIFEST_RECOVERY_SUFFIX}"
        ),
    )


def _parent_chain_safe(destination: Path) -> bool:
    """Real non-symlink writable/executable parent chain for one file.

    Every path component from the root down to the immediate parent
    must be a real directory (``lstat``, so a symlinked component is
    rejected) and the immediate parent must be writable and executable.
    """

    parent = destination.parent
    chain = [parent]
    chain.extend(parent.parents)
    for candidate in reversed(chain):
        try:
            observed = os.lstat(candidate)
        except (OSError, ValueError):
            return False
        if not stat.S_ISDIR(observed.st_mode):
            return False
    try:
        observed = os.lstat(parent)
    except (OSError, ValueError):
        return False
    if not (
        observed.st_mode
        & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        and observed.st_mode
        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    ):
        return False
    return True


def _artifact_parent_proof(
    destination: Path,
    expected_identity: tuple[int, int] | None,
) -> str | None:
    """One strict identity-bound proof of the immediate parent chain.

    Returns ``None`` only when the full parent chain is real/
    non-symlink/writable/executable and the current immediate-parent
    device/inode still equals the exact identity recorded at issued
    registration; any other observation returns a stable blocker code.
    A missing legacy identity fails closed and is never inferred as new
    authority.
    """

    if expected_identity is None:
        return "RECOVERY.EXPORT_PARENT_IDENTITY_MISSING"
    if not _parent_chain_safe(destination):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    try:
        observed = os.lstat(destination.parent)
    except (OSError, ValueError):
        return "RECOVERY.EXPORT_PARENT_UNSAFE"
    if (observed.st_dev, observed.st_ino) != expected_identity:
        return "RECOVERY.EXPORT_PARENT_REPLACED"
    return None


def _open_recovery_parent_chain_no_follow(destination: Path) -> int:
    """Open ``destination.parent`` component-by-component from the root.

    Every pathname component from the filesystem root down to the
    immediate parent is opened with ``O_DIRECTORY|O_NOFOLLOW`` against
    the previously retained descriptor, so a symlinked, missing or
    non-directory component (including an ancestor swapped to a symlink
    between an earlier validation and this walk) fails the open instead
    of redirecting the descriptor into another tree.  The caller owns
    the returned descriptor.  Only usable where ``dir_fd``-relative
    opens are available; the caller maps any unsupported-platform
    failure to its fail-closed code.
    """

    parent = destination.parent
    if not parent.is_absolute():
        raise OSError("recovery parent path is not absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(parent.parts[0], flags)
    try:
        for part in parent.parts[1:]:
            next_descriptor = os.open(
                part,
                flags,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _RecoveryParentHandle:
    """Component-bound no-follow parent dirfd for terminal recovery.

    Binding walks every component of the advertised parent pathname
    from the filesystem root with ``O_DIRECTORY|O_NOFOLLOW`` per
    component (one retained descriptor per level), so an ancestor
    swapped to a symlink between the row validation and the cleanup can
    never redirect the retained descriptor into another tree.  The
    final descriptor must be a real writable/executable directory whose
    exact device/inode equals the durable handoff identity recorded at
    issued registration.  Every terminal artifact probe,
    reconstruction open/replace, cleanup unlink and the parent fsync
    resolve only deterministic basenames relative to this descriptor;
    the advertised parent pathname is never re-resolved for destructive
    work.  ``reprove`` re-walks the advertised pathname component-wise
    and requires it to still resolve to the retained identity, raising
    ``RecoveryError`` with the stable post-validation codes
    (``RECOVERY.EXPORT_PARENT_REPLACED`` for a replaced real directory,
    ``RECOVERY.CLEANUP_FAILED`` for an unsafe chain) so the caller
    keeps the handoff and fails closed.  The raw descriptor is private.
    """

    __slots__ = (
        "destination",
        "identity",
        "_descriptor",
        "_closed",
    )

    def __init__(
        self,
        destination: Path,
        descriptor: int,
        identity: tuple[int, int],
    ) -> None:
        if type(destination) is not _NATIVE_PATH_TYPE:
            raise TypeError("destination must be an exact native Path")
        if type(descriptor) is not int or descriptor < 0:
            raise TypeError("parent descriptor must be a non-negative int")
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or type(identity[0]) is not int
            or type(identity[1]) is not int
        ):
            raise TypeError("parent identity must be a device/inode pair")
        self.destination = destination
        self._descriptor = descriptor
        self.identity = identity
        self._closed = False

    @classmethod
    def bind(
        cls,
        destination: Path,
        expected_identity: tuple[int, int],
    ) -> _RecoveryParentHandle:
        """Validate the full parent chain and retain one no-follow dirfd.

        The component-wise walk fails closed with
        ``RECOVERY.EXPORT_PARENT_UNSAFE`` on any missing, symlinked,
        non-directory or unsupported component (including platforms
        without component-safe binding) and
        ``RECOVERY.EXPORT_PARENT_REPLACED`` when the walked directory's
        exact device/inode differs from the durable handoff identity;
        a missing legacy identity fails closed with
        ``RECOVERY.EXPORT_PARENT_IDENTITY_MISSING``.
        """

        if (
            type(expected_identity) is not tuple
            or len(expected_identity) != 2
            or type(expected_identity[0]) is not int
            or type(expected_identity[1]) is not int
        ):
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_IDENTITY_MISSING",
                retryable=False,
            )
        if not (
            hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
        ):
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_UNSAFE",
                retryable=False,
            )
        descriptor = -1
        try:
            descriptor = _open_recovery_parent_chain_no_follow(
                destination
            )
            observed = os.fstat(descriptor)
        except (OSError, TypeError, ValueError):
            if descriptor >= 0:
                os.close(descriptor)
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_UNSAFE",
                retryable=False,
            ) from None
        try:
            if not stat.S_ISDIR(observed.st_mode):
                raise RecoveryError(
                    "RECOVERY.EXPORT_PARENT_UNSAFE",
                    retryable=False,
                )
            if not (
                observed.st_mode
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                and observed.st_mode
                & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            ):
                raise RecoveryError(
                    "RECOVERY.EXPORT_PARENT_UNSAFE",
                    retryable=False,
                )
            if (observed.st_dev, observed.st_ino) != expected_identity:
                raise RecoveryError(
                    "RECOVERY.EXPORT_PARENT_REPLACED",
                    retryable=False,
                )
        except RecoveryError:
            os.close(descriptor)
            raise
        return cls(
            destination,
            descriptor,
            (observed.st_dev, observed.st_ino),
        )

    @property
    def descriptor(self) -> int:
        """Private descriptor access for dirfd-relative syscalls."""

        if self._closed:
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                retryable=False,
            )
        return self._descriptor

    def fsync(self) -> None:
        """Fsync the retained directory descriptor for durability."""

        try:
            os.fsync(self.descriptor)
        except OSError:
            raise RecoveryError(
                "RECOVERY.EXPORT_IO_FAILED",
                retryable=False,
            ) from None

    def reprove(self) -> None:
        """Re-walk the advertised pathname and require the identity.

        The advertised full parent pathname is re-proven
        component-by-component with ``O_DIRECTORY|O_NOFOLLOW`` per
        component, so an ancestor symlink can never be accepted as the
        proven chain.  A replaced real directory raises
        ``RECOVERY.EXPORT_PARENT_REPLACED`` (non-retryable) and an
        unsafe chain raises ``RECOVERY.CLEANUP_FAILED`` (retryable), so
        the caller keeps the handoff and fails closed without touching
        the moved/replaced parent or any attacker path.
        """

        descriptor = -1
        try:
            descriptor = _open_recovery_parent_chain_no_follow(
                self.destination
            )
            observed = os.fstat(descriptor)
        except (OSError, TypeError, ValueError):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            ) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not stat.S_ISDIR(observed.st_mode):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            )
        if not (
            observed.st_mode
            & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            and observed.st_mode
            & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        ):
            raise RecoveryError(
                "RECOVERY.CLEANUP_FAILED",
                retryable=True,
            )
        if (observed.st_dev, observed.st_ino) != self.identity:
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                retryable=False,
            )

    def open(
        self,
        name: str,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        """Open one basename relative to the retained parent descriptor."""

        _require_recovery_basename(name)
        return os.open(
            name,
            flags,
            dir_fd=self.descriptor,
            mode=mode,
        )

    def lstat(self, name: str) -> os.stat_result:
        """No-follow stat of one basename relative to the retained fd."""

        _require_recovery_basename(name)
        return os.stat(
            name,
            dir_fd=self.descriptor,
            follow_symlinks=False,
        )

    def unlink(self, name: str) -> None:
        """Unlink one basename relative to the retained parent descriptor."""

        _require_recovery_basename(name)
        os.unlink(name, dir_fd=self.descriptor)

    def replace(self, source: str, destination: str) -> None:
        """Rename one basename to another relative to the retained fd."""

        _require_recovery_basename(source)
        _require_recovery_basename(destination)
        os.replace(
            source,
            destination,
            src_dir_fd=self.descriptor,
            dst_dir_fd=self.descriptor,
        )

    def close(self) -> None:
        """Best-effort close of the retained descriptor."""

        if self._closed:
            return
        self._closed = True
        try:
            os.close(self._descriptor)
        except OSError:
            pass

    def __enter__(self) -> _RecoveryParentHandle:
        return self

    def __exit__(
        self,
        _exc_type: object,
        _exc_value: object,
        _traceback: object,
    ) -> None:
        self.close()


def _require_recovery_basename(name: str) -> None:
    """One deterministic artifact name must be a pure safe basename."""

    if (
        type(name) is not str
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
    ):
        raise ValueError("recovery artifact name is not a safe basename")


def _after_recovery_parent_bound(
    destination: Path,
    parent_identity: tuple[int, int],
) -> None:
    """Late-bound seam invoked once the recovery parent dirfd is bound.

    Test-only fault-injection point: runs immediately after the parent
    descriptor has been bound and identity-proven against the durable
    handoff and before any descriptor-relative artifact probe or
    reconstruction, so a hostile parent rename/replacement exactly at
    that boundary can be reproduced without re-resolving the parent
    pathname.
    """


def _after_recovery_manifest_source_proved(
    destination: Path,
    manifest_temp_name: str,
    manifest_name: str,
    expected_source_identity: tuple[int, int],
) -> None:
    """Late-bound seam invoked immediately before the manifest replace.

    Runs after the exact handed-off manifest temp identity has been
    re-proven and immediately before the parent-relative rename, so a
    source-name swap exactly at that mutation boundary can be
    reproduced deterministically without re-resolving the parent
    pathname for destructive work.  Test-only fault-injection point.
    """


def _prove_recovery_manifest_source(
    manifest_temp: Path,
    manifest_temp_name: str,
    *,
    expected_digest: str,
    expected_identity: tuple[int, int],
    parent: _RecoveryParentHandle | None = None,
    parent_capture: Callable[..., _RecoveryFileCapture] | None = None,
) -> None:
    """Prove the durable manifest temp by exact digest AND handed-off inode.

    A same-byte foreign inode swapped into the temp slot after an
    earlier proof fails closed with ``RECOVERY.MANIFEST_TEMP_INVALID``
    before any rename; with a retained parent handle the capture is
    descriptor-relative.
    """

    if parent is not None:
        capture = (parent_capture or _recovery_parent_capture)(
            parent,
            manifest_temp_name,
            "RECOVERY_MANIFEST_TEMP",
        )
    else:
        capture = _capture_activation_file(
            manifest_temp,
            asset_kind="RECOVERY_MANIFEST_TEMP",
        )
    if capture.digest != expected_digest or (
        capture.identity.device,
        capture.identity.inode,
    ) != expected_identity:
        raise RecoveryError(
            "RECOVERY.MANIFEST_TEMP_INVALID",
            retryable=False,
        )


def _prove_recovery_manifest_destination(
    manifest_path: Path,
    *,
    expected_manifest_digest: str | None,
    handoff: _ArtifactHandoffFacts,
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """Prove the manifest destination against the durable handoff record.

    The destination must still match the exact prior manifest digest
    AND handed-off inode, or the recorded exact absence; any divergence
    -- including a same-byte foreign inode swapped in after an earlier
    proof -- fails closed with the existing
    ``RECOVERY.MANIFEST_DESTINATION_*`` codes before any rename, so a
    concurrent destination replacement in the late seam can never be
    silently overwritten.
    """

    if expected_manifest_digest is None:
        if handoff.prior_jsonl_absent is not True:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )
        if handoff.prior_manifest_absent is not True:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )
        try:
            if parent is not None:
                parent.lstat(manifest_path.name)
            else:
                os.lstat(manifest_path)
        except FileNotFoundError:
            pass
        except OSError as error:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNSAFE",
                retryable=False,
            ) from error
        else:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_CHANGED",
                retryable=False,
            )
    else:
        if (
            handoff.prior_manifest_absent is not False
            or handoff.prior_manifest_digest
            != expected_manifest_digest
            or handoff.prior_manifest_identity is None
        ):
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )
        state, digest, identity = _strict_file_state(
            manifest_path,
            parent=parent,
        )
        if state != "present" or digest != expected_manifest_digest:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_CHANGED",
                retryable=False,
            )
        if identity != handoff.prior_manifest_identity:
            raise RecoveryError(
                "RECOVERY.MANIFEST_DESTINATION_UNPROVEN",
                retryable=False,
            )


def _strict_file_state(
    path: Path,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """One descriptor-based no-follow proof of a configured pair entry.

    Returns ``("absent", None, None)`` when the path does not exist,
    ``("unsafe", None, None)`` when the path is a symlink, directory,
    multi-link entry, unreadable, or whose terminal identity is not
    stable, and ``("present", digest, identity)`` only for a regular
    single-link file whose bytes are hashed through the descriptor and
    whose terminal ``lstat`` still reports the same device/inode.
    Pathname hashing is never used, so a foreign same-byte inode cannot
    masquerade as a stable owned entry.  With a retained parent handle
    the probe resolves the basename relative to that descriptor and
    never re-resolves the parent pathname.
    """

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        if parent is not None:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | no_follow,
                dir_fd=parent.descriptor,
            )
        else:
            descriptor = os.open(path, os.O_RDONLY | no_follow)
    except FileNotFoundError:
        return ("absent", None, None)
    except OSError:
        return ("unsafe", None, None)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            return ("unsafe", None, None)
        identity = (observed.st_dev, observed.st_ino)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        return ("unsafe", None, None)
    finally:
        os.close(descriptor)
    try:
        if parent is not None:
            final = os.stat(
                path.name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        else:
            final = os.lstat(path)
    except OSError:
        return ("unsafe", None, None)
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != identity
    ):
        return ("unsafe", None, None)
    return ("present", digest.hexdigest(), identity)


def _manifest_for_receipt(
    receipt: SnapshotReceipt,
    kind: SnapshotKind,
) -> SnapshotManifest:
    """Deterministic adjacent manifest reconstructed from one receipt."""

    return SnapshotManifest(
        manifest_version=SNAPSHOT_MANIFEST_VERSION,
        snapshot_kind=kind,
        receipt=receipt,
        receipt_digest=snapshot_receipt_digest(receipt),
    )


def _manifest_bytes(manifest: SnapshotManifest) -> bytes:
    return contract_to_json(manifest).encode("utf-8")


def _manifest_digest_for_receipt(receipt: SnapshotReceipt) -> str:
    return hashlib.sha256(
        _manifest_bytes(
            _manifest_for_receipt(receipt, SnapshotKind.EXPLICIT_EXPORT)
        )
    ).hexdigest()


def _artifact_expected_identity(
    handoff: _ArtifactHandoffFacts | None,
    artifact: str,
) -> tuple[int, int] | None:
    """The durably handed-off identity for one artifact, if any."""

    if handoff is None:
        return None
    return {
        "jsonl_temp": handoff.jsonl_temp_identity,
        "manifest_temp": handoff.manifest_temp_identity,
        "jsonl_recovery": handoff.jsonl_recovery_identity,
        "manifest_recovery": handoff.manifest_recovery_identity,
    }[artifact]


def _unproven_artifact_code(
    handoff: _ArtifactHandoffFacts | None,
    expectations: tuple[tuple[Path, set[str], str], ...],
    *,
    parent: _RecoveryParentHandle | None = None,
) -> str | None:
    """Blocking error when an existing artifact matches expected content
    but has no durable ownership proof.

    Content equality alone is never ownership: a same-byte foreign
    inode without the exact handed-off identity is unprovable and must
    be preserved, never unlinked or replaced.  With a retained parent
    handle the probe resolves the basename relative to that descriptor.
    """

    for path, digests, artifact in expectations:
        state, digest, identity = _strict_file_state(path, parent=parent)
        if state == "absent":
            continue
        if state != "present" or digest not in digests:
            continue
        expected_identity = _artifact_expected_identity(
            handoff,
            artifact,
        )
        if expected_identity is None or identity != expected_identity:
            return "RECOVERY.ARTIFACT_UNPROVEN"
    return None


def _prior_handoff_digests(
    handoff: _ArtifactHandoffFacts | None,
    artifact: str,
) -> set[str]:
    """The durably recorded prior-pair digest set for one recovery copy.

    Only a recorded present prior entry (``absent=False`` with a
    64-hex digest) yields a digest; an unrecorded or recorded-absent
    prior state yields an empty set so an existing copy can never be
    proven and fails closed.
    """

    if handoff is None:
        return set()
    if artifact == "jsonl_recovery":
        digest = handoff.prior_jsonl_digest
        if handoff.prior_jsonl_absent is False and digest is not None:
            return {digest}
        return set()
    if artifact == "manifest_recovery":
        digest = handoff.prior_manifest_digest
        if handoff.prior_manifest_absent is False and digest is not None:
            return {digest}
        return set()
    raise ValueError(f"unknown recovery artifact {artifact!r}")


def _remove_owned_recovery_artifact(
    parent: _RecoveryParentHandle,
    name: str,
    expected_identity: _ActivationFileIdentity,
) -> bool:
    """Remove exactly one owned deterministic artifact relative to the
    retained parent descriptor; True only when provably absent after.

    The unlink, parent fsync and absence re-proof all resolve the
    basename relative to the retained descriptor, so a parent renamed
    or replaced after the bind can never redirect the unlink into the
    replacement directory.  Any observation other than the exact
    single-link owned inode returns False and the caller fails closed.
    """

    try:
        observed = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino)
        != (expected_identity.device, expected_identity.inode)
    ):
        return False
    try:
        os.unlink(name, dir_fd=parent.descriptor)
    except OSError:
        return False
    try:
        os.fsync(parent.descriptor)
    except OSError:
        return False
    try:
        os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


@dataclass(frozen=True)
class _RecoveryFileCapture:
    """Strict descriptor-relative capture of one recovery entry."""

    asset_kind: str
    identity: _ActivationFileIdentity
    digest: str


def _recovery_parent_capture(
    parent: _RecoveryParentHandle,
    name: str,
    asset_kind: str,
) -> _RecoveryFileCapture:
    """One strict single-link capture relative to the retained parent.

    Mirrors the activation-journal capture semantics (regular
    single-link closure at the initial stat, the descriptor fstat/read
    and the final revalidation) but resolves the entry only relative to
    the retained descriptor, so a swapped parent pathname can never
    substitute a foreign entry.
    """

    try:
        initial = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | no_follow,
            dir_fd=parent.descriptor,
        )
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise ActivationPreparationError(
                "ACTIVATION.PRIOR_ASSET_INVALID",
                retryable=False,
            )
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        identity = _ActivationFileIdentity(
            observed.st_dev,
            observed.st_ino,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        final = os.stat(
            name,
            dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        ) from error
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino)
        != (identity.device, identity.inode)
    ):
        raise ActivationPreparationError(
            "ACTIVATION.PRIOR_ASSET_INVALID",
            retryable=False,
        )
    return _RecoveryFileCapture(
        asset_kind=asset_kind,
        identity=identity,
        digest=digest.hexdigest(),
    )


def _recovery_parent_open_exclusive(
    parent: _RecoveryParentHandle,
    name: str,
) -> tuple[int, _ActivationFileIdentity]:
    """Create one exclusive deterministic temporary relative to the
    retained parent descriptor; returns the open descriptor and the
    exact created identity."""

    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(
        name,
        flags,
        0o600,
        dir_fd=parent.descriptor,
    )
    try:
        observed = os.fstat(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        os.close(descriptor)
        raise OSError("recovery temporary is not an exclusive regular file")
    return descriptor, _ActivationFileIdentity(
        observed.st_dev,
        observed.st_ino,
    )


def _remove_content_proven_artifact(
    path: Path,
    expected_digest: str,
    *,
    expected_identity: tuple[int, int] | None,
    parent: _RecoveryParentHandle | None = None,
) -> bool:
    """Remove one deterministic artifact proven by strict content proof
    plus the durable handed-off identity.

    The unlink is identity-bound to the proven inode with a parent
    fsync and an absence re-proof.  A foreign, linked, unsafe,
    digest-mismatched or identity-less entry fails closed and is never
    removed.  With a retained parent handle the probe, unlink, fsync
    and absence re-proof resolve the basename relative to that
    descriptor and never re-resolve the parent pathname.
    """

    state, digest, identity = _strict_file_state(
        path,
        parent=parent,
    )
    if state == "absent":
        return True
    if (
        state != "present"
        or digest != expected_digest
        or expected_identity is None
        or identity is None
        or identity != expected_identity
    ):
        return False
    if parent is not None:
        return _remove_owned_recovery_artifact(
            parent,
            path.name,
            _ActivationFileIdentity(identity[0], identity[1]),
        )
    return _remove_owned_activation_journal_temp(
        path,
        _ActivationFileIdentity(identity[0], identity[1]),
    )


def _recovery_cleanup_parent_gate(
    destination: Path,
    parent: _RecoveryParentHandle | None,
) -> None:
    """Fail closed before any destructive probe when the advertised
    parent pathname can no longer be proven.

    Runs after the row validation, before the first descriptor-relative
    probe or unlink.  A replaced real directory fails closed with
    ``RECOVERY.EXPORT_PARENT_REPLACED`` without touching either the
    moved original or the replacement, and an unsafe chain (symlinked,
    missing, unwritable) fails closed with ``RECOVERY.CLEANUP_FAILED``
    so the cleanup-pending handoff is kept for a later idempotent
    replay.
    """

    if parent is not None:
        parent.reprove()


def _fsync_artifact_parent(
    destination: Path,
    expected_identity: tuple[int, int] | None,
    *,
    parent: _RecoveryParentHandle | None = None,
) -> None:
    """No-follow directory-descriptor proof and fsync of the parent.

    The parent directory is opened with ``O_DIRECTORY`` and
    ``O_NOFOLLOW`` (where available), its descriptor is ``fstat``-proven
    to be the exact device/inode recorded at issued registration, the
    descriptor is fsynced, and the pathname/full chain is re-proven to
    still resolve to that same identity.  The fsync is unconditional:
    even when every deterministic artifact is already absent (a prior
    unlink succeeded but its directory fsync failed), the release must
    still durably record the absence before the cleanup-pending handoff
    journal is cleared.  Any mismatch or failure raises
    ``RecoveryError`` and the caller keeps the handoff and reports
    BLOCKED; a missing legacy identity fails closed and is never
    inferred as new authority.  With a retained parent handle the fsync
    runs on that exact retained descriptor and the advertised pathname
    is re-proven by the component-wise walk; the parent pathname is
    never re-resolved for the fsync.
    """

    if parent is not None:
        parent.fsync()
        parent.reprove()
        return
    proof_error = _artifact_parent_proof(destination, expected_identity)
    if proof_error is not None:
        raise RecoveryError(proof_error, retryable=False)
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(destination.parent, flags)
        observed = os.fstat(descriptor)
        if not stat.S_ISDIR(observed.st_mode) or (
            observed.st_dev,
            observed.st_ino,
        ) != expected_identity:
            raise RecoveryError(
                "RECOVERY.EXPORT_PARENT_REPLACED",
                retryable=False,
            )
        os.fsync(descriptor)
    except RecoveryError:
        raise
    except OSError:
        raise RecoveryError(
            "RECOVERY.EXPORT_IO_FAILED",
            retryable=False,
        ) from None
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    proof_error = _artifact_parent_proof(destination, expected_identity)
    if proof_error is not None:
        raise RecoveryError(proof_error, retryable=False)


def _strict_pair_file_state(
    path: Path,
) -> tuple[str, str | None, tuple[int, int] | None]:
    """One descriptor-based no-follow proof of a configured pair entry.

    Returns ``("absent", None, None)`` when the path does not exist,
    ``("unsafe", None, None)`` when the path is a symlink, directory,
    multi-link entry, unreadable, or whose terminal identity is not
    stable, and ``("present", digest, identity)`` only for a regular
    single-link file whose bytes are hashed through the descriptor and
    whose terminal ``lstat`` still reports the same device/inode.
    Pathname hashing is never used, so a foreign same-byte inode cannot
    masquerade as a stable owned entry.
    """

    no_follow = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | no_follow)
    except FileNotFoundError:
        return ("absent", None, None)
    except OSError:
        return ("unsafe", None, None)
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            return ("unsafe", None, None)
        identity = (observed.st_dev, observed.st_ino)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    except OSError:
        return ("unsafe", None, None)
    finally:
        os.close(descriptor)
    try:
        final = os.lstat(path)
    except OSError:
        return ("unsafe", None, None)
    if (
        not stat.S_ISREG(final.st_mode)
        or final.st_nlink != 1
        or (final.st_dev, final.st_ino) != identity
    ):
        return ("unsafe", None, None)
    return ("present", digest.hexdigest(), identity)


def _artifact_parent_dirfd(
    destination: Path,
    expected_identity: tuple[int, int] | None,
    *,
    error_factory: Callable[[str], Exception],
) -> int:
    """Open and fstat-prove one no-follow parent directory descriptor.

    Runs the real non-symlink writable/executable parent-chain proof,
    opens the immediate parent with ``O_DIRECTORY|O_NOFOLLOW`` (where
    available) and requires the descriptor's exact device/inode to equal
    the durable handoff identity recorded at issued registration.  A
    missing legacy identity, an unsafe/replaced chain, an open/fstat
    failure or an identity mismatch fails closed with a stable store
    code and never returns a descriptor.
    """

    if expected_identity is None:
        raise error_factory(
            "STORE.HANDOFF_PARENT_IDENTITY_MISSING"
        )
    if not _parent_chain_safe(destination):
        raise error_factory("STORE.HANDOFF_PARENT_UNSAFE")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(destination.parent, flags)
    except OSError:
        raise error_factory(
            "STORE.HANDOFF_PARENT_UNSAFE"
        ) from None
    try:
        observed = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise error_factory(
            "STORE.HANDOFF_PARENT_UNSAFE"
        ) from None
    if (
        not stat.S_ISDIR(observed.st_mode)
        or (observed.st_dev, observed.st_ino) != expected_identity
    ):
        os.close(descriptor)
        raise error_factory("STORE.HANDOFF_PARENT_REPLACED")
    return descriptor


def _after_artifact_parent_dirfd_bound(
    destination: Path,
    parent_identity: tuple[int, int] | None,
) -> None:
    """Late-bound seam invoked once the handoff parent dirfd is bound.

    Test-only fault-injection point: runs immediately after the parent
    descriptor has been opened and fstat-proven against the durable
    handoff identity and before any descriptor-relative artifact probe,
    so a hostile parent rename/replacement exactly at that boundary can
    be reproduced without re-resolving the parent pathname.
    """


def _artifact_handoff_dirfd_entry(
    name: str,
    descriptor: int,
    expected_identity: tuple[int, int],
    code: str,
    *,
    error_factory: Callable[[str], Exception],
) -> None:
    """Prove one deterministic handoff entry relative to a parent dirfd.

    The basename must exist under the retained descriptor as a regular
    single-link file whose exact device/inode equals the handed-off
    identity; absent, symlinked, directory, multi-link, swapped or
    unreadable entries fail closed with ``code``.
    """

    try:
        observed = os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise error_factory(code) from None
    except OSError:
        raise error_factory(code) from None
    if (
        not stat.S_ISREG(observed.st_mode)
        or observed.st_nlink != 1
        or (observed.st_dev, observed.st_ino) != expected_identity
    ):
        raise error_factory(code)


def _artifact_handoff_dirfd_absent(
    name: str,
    descriptor: int,
) -> bool:
    """True only when one basename is provably absent under the dirfd."""

    try:
        os.stat(
            name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        return False
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _artifact_handoff_dirfd_reprove(
    destination: Path,
    expected_identity: tuple[int, int],
    *,
    error_factory: Callable[[str], Exception],
) -> None:
    """Re-prove the advertised full parent pathname still resolves to
    the exact handoff identity after the descriptor-relative probes."""

    if not _parent_chain_safe(destination):
        raise error_factory("STORE.HANDOFF_PARENT_UNSAFE")
    try:
        observed = os.lstat(destination.parent)
    except (OSError, ValueError):
        raise error_factory(
            "STORE.HANDOFF_PARENT_UNSAFE"
        ) from None
    if (observed.st_dev, observed.st_ino) != expected_identity:
        raise error_factory("STORE.HANDOFF_PARENT_REPLACED")


def _require_artifact_identity_pair(
    value: object,
    field_name: str,
) -> None:
    """Validate one optional exact ``(device, inode)`` identity pair."""

    if value is None:
        return
    if (
        type(value) is not tuple
        or len(value) != 2
        or type(value[0]) is not int
        or type(value[1]) is not int
    ):
        raise ValueError(f"{field_name} is invalid")


_ARTIFACT_HANDOFF_META_PREFIX = "artifact_handoff."

_ARTIFACT_HANDOFF_META_KEYS = frozenset(
    {
        "version",
        "artifact_parent_device",
        "artifact_parent_inode",
        "jsonl_temp_device",
        "jsonl_temp_inode",
        "manifest_temp_device",
        "manifest_temp_inode",
        "jsonl_recovery_device",
        "jsonl_recovery_inode",
        "manifest_recovery_device",
        "manifest_recovery_inode",
        "prior_jsonl_device",
        "prior_jsonl_inode",
        "prior_jsonl_digest",
        "prior_jsonl_absent",
        "prior_manifest_device",
        "prior_manifest_inode",
        "prior_manifest_digest",
        "prior_manifest_absent",
    }
)


def _artifact_handoff_meta_key(snapshot_id: str) -> str:
    return f"{_ARTIFACT_HANDOFF_META_PREFIX}{snapshot_id}"


def _artifact_handoff_prior_record(
    *,
    identity: tuple[int, int] | None,
    digest: str | None,
    absent: bool | None,
    field_name: str,
) -> None:
    """Validate one optional durable prior-pair asset record.

    The record is either an explicit proven absence (``absent=True``
    with no digest/identity), a proven present entry (``absent=False``
    with a 64-hex digest and an exact device/inode pair), or entirely
    unrecorded (``absent=None`` with no digest/identity).  Anything else
    is rejected so reconstruction and cleanup never act on a half-written
    ownership record.
    """

    if absent is True:
        if digest is not None or identity is not None:
            raise ValueError(f"{field_name} absent record must be empty")
        return
    if absent is False:
        if type(digest) is not str or len(digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in digest
        ):
            raise ValueError(f"{field_name} digest is invalid")
        if (
            type(identity) is not tuple
            or len(identity) != 2
            or type(identity[0]) is not int
            or type(identity[1]) is not int
        ):
            raise ValueError(f"{field_name} identity is invalid")
        return
    if absent is None:
        if digest is not None or identity is not None:
            raise ValueError(f"{field_name} record is not fully absent")
        return
    raise ValueError(f"{field_name} absent flag is invalid")


def _artifact_handoff_meta_value(
    *,
    jsonl_temp_identity: tuple[int, int] | None,
    manifest_temp_identity: tuple[int, int] | None,
    artifact_parent_identity: tuple[int, int] | None = None,
    jsonl_recovery_identity: tuple[int, int] | None = None,
    manifest_recovery_identity: tuple[int, int] | None = None,
    prior_jsonl_identity: tuple[int, int] | None = None,
    prior_jsonl_digest: str | None = None,
    prior_jsonl_absent: bool | None = None,
    prior_manifest_identity: tuple[int, int] | None = None,
    prior_manifest_digest: str | None = None,
    prior_manifest_absent: bool | None = None,
) -> str:
    """One write-once artifact handoff cleanup-pending journal payload.

    In addition to the exclusive temporary and recovery-copy identities,
    the payload durably records the exact immediate-parent device/inode
    (``artifact_parent_identity``) proven by a strict real non-symlink
    parent-chain proof at issued registration, so terminal cleanup and
    handoff release can detect a hostile parent rename or replacement
    and fail closed with the handoff retained.  The payload also
    durably records the prior final JSONL/manifest identity and digest
    (or their explicit proven absence) captured before any publication
    replace, so reconstruction can replace an old manifest only when
    the current entry still matches the recorded prior identity and
    digest.  Missing prior records are ``None`` and always fail closed.
    """

    _require_artifact_identity_pair(
        artifact_parent_identity,
        "artifact_parent_identity",
    )
    _artifact_handoff_prior_record(
        identity=prior_jsonl_identity,
        digest=prior_jsonl_digest,
        absent=prior_jsonl_absent,
        field_name="prior_jsonl",
    )
    _artifact_handoff_prior_record(
        identity=prior_manifest_identity,
        digest=prior_manifest_digest,
        absent=prior_manifest_absent,
        field_name="prior_manifest",
    )
    return json.dumps(
        {
            "version": 1,
            "artifact_parent_device": (
                None
                if artifact_parent_identity is None
                else artifact_parent_identity[0]
            ),
            "artifact_parent_inode": (
                None
                if artifact_parent_identity is None
                else artifact_parent_identity[1]
            ),
            "jsonl_temp_device": (
                None
                if jsonl_temp_identity is None
                else jsonl_temp_identity[0]
            ),
            "jsonl_temp_inode": (
                None
                if jsonl_temp_identity is None
                else jsonl_temp_identity[1]
            ),
            "manifest_temp_device": (
                None
                if manifest_temp_identity is None
                else manifest_temp_identity[0]
            ),
            "manifest_temp_inode": (
                None
                if manifest_temp_identity is None
                else manifest_temp_identity[1]
            ),
            "jsonl_recovery_device": (
                None
                if jsonl_recovery_identity is None
                else jsonl_recovery_identity[0]
            ),
            "jsonl_recovery_inode": (
                None
                if jsonl_recovery_identity is None
                else jsonl_recovery_identity[1]
            ),
            "manifest_recovery_device": (
                None
                if manifest_recovery_identity is None
                else manifest_recovery_identity[0]
            ),
            "manifest_recovery_inode": (
                None
                if manifest_recovery_identity is None
                else manifest_recovery_identity[1]
            ),
            "prior_jsonl_device": (
                None
                if prior_jsonl_identity is None
                else prior_jsonl_identity[0]
            ),
            "prior_jsonl_inode": (
                None
                if prior_jsonl_identity is None
                else prior_jsonl_identity[1]
            ),
            "prior_jsonl_digest": prior_jsonl_digest,
            "prior_jsonl_absent": prior_jsonl_absent,
            "prior_manifest_device": (
                None
                if prior_manifest_identity is None
                else prior_manifest_identity[0]
            ),
            "prior_manifest_inode": (
                None
                if prior_manifest_identity is None
                else prior_manifest_identity[1]
            ),
            "prior_manifest_digest": prior_manifest_digest,
            "prior_manifest_absent": prior_manifest_absent,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _artifact_handoff_from_meta(
    key: str,
    value: str,
) -> _ArtifactHandoffFacts | None:
    """Strictly parse one handoff journal row, or None when unprovable."""

    if not key.startswith(_ARTIFACT_HANDOFF_META_PREFIX):
        return None
    snapshot_id = key[len(_ARTIFACT_HANDOFF_META_PREFIX) :]
    if not snapshot_id:
        return None
    def reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        for field, item in pairs:
            if field in payload:
                raise ValueError("artifact handoff contains duplicate fields")
            payload[field] = item
        return payload

    try:
        payload = json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"non-finite artifact handoff value: {token}")
            ),
        )
    except (TypeError, ValueError):
        return None
    if (
        type(payload) is not dict
        or set(payload) != _ARTIFACT_HANDOFF_META_KEYS
        or type(payload["version"]) is not int
        or payload["version"] != 1
    ):
        return None

    def identity(device_key: str, inode_key: str) -> tuple[int, int] | None:
        device = payload[device_key]
        inode = payload[inode_key]
        if device is None and inode is None:
            return None
        if type(device) is not int or type(inode) is not int:
            raise ValueError("artifact handoff identity is invalid")
        return (device, inode)

    def prior_record(
        *,
        identity_key: str,
        digest_key: str,
        absent_key: str,
    ) -> tuple[tuple[int, int] | None, str | None, bool | None]:
        """One strict prior-asset record, or None/Nones when unrecorded."""

        absent_value = payload[absent_key]
        if absent_value is None:
            inode_key = identity_key.removesuffix("_device") + "_inode"
            if (
                payload[identity_key] is not None
                or payload[inode_key] is not None
                or payload[digest_key] is not None
            ):
                raise ValueError("artifact handoff prior record is partial")
            return (None, None, None)
        if type(absent_value) is not bool:
            raise ValueError("artifact handoff prior absent flag is invalid")
        digest = payload[digest_key]
        if absent_value:
            inode_key = identity_key.removesuffix("_device") + "_inode"
            if (
                digest is not None
                or payload[identity_key] is not None
                or payload[inode_key] is not None
            ):
                raise ValueError("artifact handoff absent record is not empty")
            return (None, None, True)
        if type(digest) is not str or len(digest) != 64 or any(
            character not in "0123456789abcdef"
            for character in digest
        ):
            raise ValueError("artifact handoff prior digest is invalid")
        inode_key = identity_key.removesuffix("_device") + "_inode"
        prior_identity = identity(identity_key, inode_key)
        if prior_identity is None:
            raise ValueError("artifact handoff prior identity is missing")
        return (prior_identity, digest, False)

    try:
        prior_jsonl = prior_record(
            identity_key="prior_jsonl_device",
            digest_key="prior_jsonl_digest",
            absent_key="prior_jsonl_absent",
        )
        prior_manifest = prior_record(
            identity_key="prior_manifest_device",
            digest_key="prior_manifest_digest",
            absent_key="prior_manifest_absent",
        )
        return _ArtifactHandoffFacts(
            snapshot_id=snapshot_id,
            jsonl_temp_identity=identity(
                "jsonl_temp_device",
                "jsonl_temp_inode",
            ),
            artifact_parent_identity=identity(
                "artifact_parent_device",
                "artifact_parent_inode",
            ),
            manifest_temp_identity=identity(
                "manifest_temp_device",
                "manifest_temp_inode",
            ),
            jsonl_recovery_identity=identity(
                "jsonl_recovery_device",
                "jsonl_recovery_inode",
            ),
            manifest_recovery_identity=identity(
                "manifest_recovery_device",
                "manifest_recovery_inode",
            ),
            prior_jsonl_identity=prior_jsonl[0],
            prior_jsonl_digest=prior_jsonl[1],
            prior_jsonl_absent=prior_jsonl[2],
            prior_manifest_identity=prior_manifest[0],
            prior_manifest_digest=prior_manifest[1],
            prior_manifest_absent=prior_manifest[2],
        )
    except (KeyError, TypeError, ValueError):
        return None
