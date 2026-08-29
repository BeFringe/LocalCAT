"""Durable receipt ledger and cold-recoverable pending operation inventory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import secrets

from platform_fs_contracts import (
    BoundDirectoryAuthority,
    CandidateFile,
    EntrySnapshot,
    FileObjectIdentity,
    LedgerEnumerationLimits,
    LockLease,
    LockPolicy,
    LockWait,
    PendingPublication,
    PlatformFileBackend,
    PlatformFileError,
    PlatformFileErrorCode,
    PublishMode,
    RootedDirectoryAuthority,
)

from resource_package_contracts import (
    ResourceImportMode,
    ResourceOperationReceipt,
    ResourcePortabilityError,
    receipt_from_bytes,
    receipt_to_bytes,
)
from resource_platform_io import platform_relative_path, read_bound_all


_PENDING_SCHEMA = "localcat-resource-pending-operation-v1"
_PENDING_KEYS = {
    "schema",
    "phase",
    "receipt_json",
    "import_mode",
    "destination_name",
    "destination_relative_path",
}
_MAX_LEDGER_ENTRY_BYTES = 1024 * 1024
_LEDGER_LIMITS = LedgerEnumerationLimits(
    maximum_entries=4096,
    maximum_name_bytes=255,
    maximum_total_bytes=64 * 1024 * 1024,
)
_LEDGER_LOCK_NAME = ".resource-ledger.lock"
_LEDGER_LOCK_PREFIX = b"localcat.resource-ledger.lock.v1\0"


class ResourcePendingPhase(str, Enum):
    ARMED = "armed"
    RECEIPT_READY = "receipt_ready"
    MANUAL_REQUIRED = "manual_required"


@dataclass(frozen=True, slots=True)
class ResourcePendingOperation:
    phase: ResourcePendingPhase
    receipt: ResourceOperationReceipt
    import_mode: ResourceImportMode | None = None
    destination_name: str | None = None
    destination_relative_path: str | None = None

    def __post_init__(self) -> None:
        if type(self.phase) is not ResourcePendingPhase:
            raise TypeError("pending phase must be exact")
        if type(self.receipt) is not ResourceOperationReceipt:
            raise TypeError("pending receipt must be exact")
        self.receipt.__post_init__()
        if self.import_mode is not None and type(self.import_mode) is not ResourceImportMode:
            raise TypeError("pending import mode must be exact or None")
        for value, label in (
            (self.destination_name, "pending destination name"),
            (self.destination_relative_path, "pending destination relative path"),
        ):
            if value is not None and (type(value) is not str or not value):
                raise TypeError(f"{label} must be nonempty str or None")
        if self.import_mode is ResourceImportMode.CREATE_NEW:
            if self.destination_name is None or self.destination_relative_path is None:
                raise ValueError("RESOURCE.RECEIPT.INVALID")
            relative = Path(self.destination_relative_path)
            if relative.is_absolute() or len(relative.parts) != 1 or relative.name in ("", ".", ".."):
                raise ValueError("RESOURCE.RECEIPT.INVALID")
        elif self.destination_name is not None or self.destination_relative_path is not None:
            raise ValueError("RESOURCE.RECEIPT.INVALID")


class ResourceReceiptLedger:
    """Persist exact receipts and pre-armed recovery facts below the safe root."""

    def __init__(
        self,
        config_dir: Path,
        backend: PlatformFileBackend | None = None,
    ) -> None:
        if type(config_dir) is not type(Path()) or not config_dir.is_absolute():
            raise TypeError("receipt config directory must be an absolute concrete Path")
        if backend is not None and not isinstance(backend, PlatformFileBackend):
            raise TypeError("receipt backend must satisfy PlatformFileBackend")
        self.root = config_dir / "resource-portability"
        self.receipt_dir = self.root / "receipts"
        self.pending_dir = self.root / "pending"
        try:
            self.receipt_dir.mkdir(parents=True, exist_ok=True)
            self.pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from None
        if backend is None:
            from platform_fs import compose_platform_file_backend

            backend = compose_platform_file_backend(self.root)
        self._backend = backend

    @property
    def backend(self) -> PlatformFileBackend:
        return self._backend

    def begin(
        self,
        receipt_template: ResourceOperationReceipt,
        *,
        import_mode: ResourceImportMode | None = None,
        destination_name: str | None = None,
        destination_relative_path: str | None = None,
    ) -> ResourcePendingOperation:
        pending = ResourcePendingOperation(
            phase=ResourcePendingPhase.ARMED,
            receipt=receipt_template,
            import_mode=import_mode,
            destination_name=destination_name,
            destination_relative_path=destination_relative_path,
        )
        self._write_entry(
            self.pending_dir,
            self._pending_name(receipt_template.operation_id),
            _pending_to_bytes(pending),
            replace=False,
        )
        return pending

    def mark_receipt_ready(
        self,
        receipt: ResourceOperationReceipt,
    ) -> ResourcePendingOperation:
        current = self.get_pending(receipt.operation_id)
        ready = ResourcePendingOperation(
            phase=ResourcePendingPhase.RECEIPT_READY,
            receipt=receipt,
            import_mode=current.import_mode,
            destination_name=current.destination_name,
            destination_relative_path=current.destination_relative_path,
        )
        self._replace_pending(ready, expected=current)
        return ready

    def mark_manual(self, operation_id: str) -> ResourcePendingOperation:
        current = self.get_pending(operation_id)
        manual = ResourcePendingOperation(
            phase=ResourcePendingPhase.MANUAL_REQUIRED,
            receipt=current.receipt,
            import_mode=current.import_mode,
            destination_name=current.destination_name,
            destination_relative_path=current.destination_relative_path,
        )
        self._replace_pending(manual, expected=current)
        return manual

    def commit(self, receipt: ResourceOperationReceipt) -> Path:
        current = self.get_pending(receipt.operation_id)
        if current.phase is not ResourcePendingPhase.RECEIPT_READY or current.receipt != receipt:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        destination = self.append(receipt)
        self.abandon(receipt.operation_id)
        return destination

    def append(self, receipt: ResourceOperationReceipt) -> Path:
        payload = receipt_to_bytes(receipt)
        destination = self._receipt_path(receipt.operation_id)
        name = destination.name
        root = None
        lease = None
        try:
            root = self._backend.bind_root(self.receipt_dir)
            lease = self._acquire_ledger(root, "receipts")
            observed = root.inspect_entry(name)
            if observed is not None:
                try:
                    existing = self._read_observed(root, name, observed)
                    if receipt_from_bytes(existing) == receipt:
                        return destination
                except (PlatformFileError, ResourcePortabilityError):
                    pass
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
            self._publish_locked(root, lease, name, payload, replace=False)
        except ResourcePortabilityError:
            raise
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from None
        finally:
            _close_authorities(lease, root)
        return destination

    def abandon(self, operation_id: str) -> None:
        name = self._pending_name(operation_id)
        root = None
        lease = None
        try:
            root = self._backend.bind_root(self.pending_dir)
            lease = self._acquire_ledger(root, "pending")
            observed = root.inspect_entry(name)
            if observed is None:
                return
            root.unlink_owned(name, observed.identity)
            if root.inspect_entry(name) is not None:
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        except ResourcePortabilityError:
            raise
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from error
        finally:
            _close_authorities(lease, root)

    def get(self, operation_id: str) -> ResourceOperationReceipt:
        try:
            return receipt_from_bytes(
                self._read_entry(
                    self.receipt_dir,
                    self._receipt_name(operation_id),
                )
            )
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from None

    def list_receipts(self) -> tuple[ResourceOperationReceipt, ...]:
        try:
            return tuple(
                receipt_from_bytes(payload)
                for _name, payload in self._list_entries(self.receipt_dir, ".json")
            )
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from None

    def get_pending(self, operation_id: str) -> ResourcePendingOperation:
        try:
            return _pending_from_bytes(
                self._read_entry(
                    self.pending_dir,
                    self._pending_name(operation_id),
                )
            )
        except PlatformFileError as error:
            if _is_unavailable(error):
                raise ResourcePortabilityError("RESOURCE.IMPORT.PREVIEW_STALE") from None
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from None

    def list_pending(self) -> tuple[ResourcePendingOperation, ...]:
        try:
            return tuple(
                _pending_from_bytes(payload)
                for _name, payload in self._list_entries(self.pending_dir, ".journal")
            )
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from None

    def _replace_pending(
        self,
        pending: ResourcePendingOperation,
        *,
        expected: ResourcePendingOperation,
    ) -> None:
        name = self._pending_name(pending.receipt.operation_id)
        root = None
        lease = None
        try:
            root = self._backend.bind_root(self.pending_dir)
            lease = self._acquire_ledger(root, "pending")
            observed = root.inspect_entry(name)
            if observed is None or _pending_from_bytes(
                self._read_observed(root, name, observed)
            ) != expected:
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
            self._publish_locked(
                root,
                lease,
                name,
                _pending_to_bytes(pending),
                replace=True,
            )
        except ResourcePortabilityError:
            raise
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from None
        finally:
            _close_authorities(lease, root)

    def _write_entry(
        self,
        directory: Path,
        name: str,
        payload: bytes,
        *,
        replace: bool,
    ) -> None:
        root = None
        lease = None
        try:
            root = self._backend.bind_root(directory)
            lease = self._acquire_ledger(root, directory.name)
            observed = root.inspect_entry(name)
            if replace and observed is None:
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
            if not replace and observed is not None:
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
            self._publish_locked(root, lease, name, payload, replace=replace)
        except ResourcePortabilityError:
            raise
        except PlatformFileError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from None
        finally:
            _close_authorities(lease, root)

    def _publish_locked(
        self,
        root: RootedDirectoryAuthority,
        lease: LockLease,
        name: str,
        payload: bytes,
        *,
        replace: bool,
    ) -> None:
        if len(payload) > _MAX_LEDGER_ENTRY_BYTES:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        candidate: CandidateFile | None = None
        pending: PendingPublication | None = None
        candidate_identity: FileObjectIdentity | None = None
        candidate_name = _candidate_name(name)
        try:
            candidate = root.create_candidate(candidate_name, private=True)
            candidate_identity = candidate.identity()
            written = candidate.write_chunks(
                _payload_chunks(payload),
                maximum_bytes=_MAX_LEDGER_ENTRY_BYTES,
            )
            candidate.flush_content()
            pending = root.begin_publish(
                candidate,
                name,
                mode=(
                    PublishMode.REPLACE_UNDER_LOCK
                    if replace
                    else PublishMode.CREATE_IF_ABSENT
                ),
                lease=lease if replace else None,
            )
            candidate = None
            candidate_identity = None
            facts = pending.preliminary_facts()
            retained = pending.retained_destination()
            snapshot = retained.snapshot()
            if (
                facts.content_sha256 != written.content_sha256
                or facts.byte_count != written.byte_count
                or snapshot.byte_count != len(payload)
                or read_bound_all(
                    retained,
                    snapshot,
                    maximum_bytes=_MAX_LEDGER_ENTRY_BYTES,
                )
                != payload
                or pending.terminal_reproof() != facts
            ):
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        finally:
            _close_authorities(pending, candidate)
            if candidate_identity is not None:
                try:
                    root.unlink_owned(candidate_name, candidate_identity)
                except PlatformFileError:
                    pass

    def _read_entry(self, directory: Path, name: str) -> bytes:
        root = None
        lease = None
        try:
            root = self._backend.bind_root(directory)
            lease = self._acquire_ledger(root, directory.name)
            observed = root.inspect_entry(name)
            if observed is None:
                raise PlatformFileError(
                    PlatformFileErrorCode.ENTRY_UNAVAILABLE,
                    retryable=False,
                )
            return self._read_observed(root, name, observed)
        finally:
            _close_authorities(lease, root)

    def _read_observed(
        self,
        root: RootedDirectoryAuthority,
        name: str,
        observed: EntrySnapshot,
    ) -> bytes:
        source = self._backend.open_regular(root, platform_relative_path(name))
        try:
            snapshot = source.snapshot()
            if snapshot != observed:
                raise PlatformFileError(
                    PlatformFileErrorCode.IDENTITY_STALE,
                    retryable=True,
                )
            return read_bound_all(
                source,
                snapshot,
                maximum_bytes=_MAX_LEDGER_ENTRY_BYTES,
            )
        finally:
            source.close()

    def _list_entries(
        self,
        directory: Path,
        suffix: str,
    ) -> tuple[tuple[str, bytes], ...]:
        root = None
        lease = None
        try:
            root = self._backend.bind_root(directory)
            lease = self._acquire_ledger(root, directory.name)
            observations = root.observe_ledger_entries(lease, _LEDGER_LIMITS)
            result: list[tuple[str, bytes]] = []
            for observation in observations:
                if not _is_owner_entry(observation.name, suffix):
                    continue
                result.append(
                    (
                        observation.name,
                        self._read_observed(
                            root,
                            observation.name,
                            observation.snapshot,
                        ),
                    )
                )
            return tuple(result)
        finally:
            _close_authorities(lease, root)

    def _acquire_ledger(
        self,
        root: BoundDirectoryAuthority,
        role: str,
    ) -> LockLease:
        payload = _LEDGER_LOCK_PREFIX + hashlib.sha256(
            role.encode("utf-8", "strict")
        ).digest()
        return self._backend.acquire(
            root,
            _LEDGER_LOCK_NAME,
            payload,
            LockPolicy(LockWait.BLOCK),
        )

    def _receipt_path(self, operation_id: str) -> Path:
        return self.receipt_dir / self._receipt_name(operation_id)

    def _pending_path(self, operation_id: str) -> Path:
        return self.pending_dir / self._pending_name(operation_id)

    @staticmethod
    def _receipt_name(operation_id: str) -> str:
        return f"{_safe_operation_id(operation_id)}.json"

    @staticmethod
    def _pending_name(operation_id: str) -> str:
        return f"{_safe_operation_id(operation_id)}.journal"


def _pending_to_bytes(pending: ResourcePendingOperation) -> bytes:
    pending.__post_init__()
    payload = {
        "schema": _PENDING_SCHEMA,
        "phase": pending.phase.value,
        "receipt_json": receipt_to_bytes(pending.receipt).decode("utf-8"),
        "import_mode": None if pending.import_mode is None else pending.import_mode.value,
        "destination_name": pending.destination_name,
        "destination_relative_path": pending.destination_relative_path,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"


def _pending_from_bytes(payload: bytes) -> ResourcePendingOperation:
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error
    if type(value) is not dict or set(value) != _PENDING_KEYS:
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID")
    try:
        if value["schema"] != _PENDING_SCHEMA or type(value["receipt_json"]) is not str:
            raise ValueError
        phase = ResourcePendingPhase(value["phase"])
        receipt = receipt_from_bytes(value["receipt_json"].encode("utf-8"))
        raw_mode = value["import_mode"]
        mode = None if raw_mode is None else ResourceImportMode(raw_mode)
        pending = ResourcePendingOperation(
            phase=phase,
            receipt=receipt,
            import_mode=mode,
            destination_name=value["destination_name"],
            destination_relative_path=value["destination_relative_path"],
        )
    except (TypeError, ValueError, ResourcePortabilityError) as error:
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error
    if _pending_to_bytes(pending) != payload:
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID")
    return pending


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _safe_operation_id(operation_id: str) -> str:
    if (
        type(operation_id) is not str
        or not operation_id
        or any(character not in "0123456789abcdefghijklmnopqrstuvwxyz-" for character in operation_id)
    ):
        raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID")
    return operation_id


def _candidate_name(destination_name: str) -> str:
    digest = hashlib.sha256(destination_name.encode("utf-8", "strict")).hexdigest()
    return f".resource-ledger-{digest[:16]}-{secrets.token_hex(8)}.tmp"


def _payload_chunks(payload: bytes) -> tuple[bytes, ...]:
    return tuple(
        payload[offset : offset + 64 * 1024]
        for offset in range(0, len(payload), 64 * 1024)
    )


def _is_owner_entry(name: str, suffix: str) -> bool:
    if type(name) is not str or type(suffix) is not str or not name.endswith(suffix):
        return False
    operation_id = name[: -len(suffix)]
    try:
        return _safe_operation_id(operation_id) == operation_id
    except ResourcePortabilityError:
        return False


def _is_unavailable(error: PlatformFileError) -> bool:
    return error.code == PlatformFileErrorCode.ENTRY_UNAVAILABLE.value


def _close_authorities(*authorities: object) -> None:
    for authority in authorities:
        if authority is not None:
            try:
                authority.close()  # type: ignore[attr-defined]
            except PlatformFileError:
                pass


__all__ = [
    "ResourcePendingOperation",
    "ResourcePendingPhase",
    "ResourceReceiptLedger",
]
