"""Durable receipt ledger and cold-recoverable pending operation inventory."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import tempfile

from resource_package_contracts import (
    ResourceImportMode,
    ResourceOperationReceipt,
    ResourcePortabilityError,
    receipt_from_bytes,
    receipt_to_bytes,
)


_PENDING_SCHEMA = "localcat-resource-pending-operation-v1"
_PENDING_KEYS = {
    "schema",
    "phase",
    "receipt_json",
    "import_mode",
    "destination_name",
    "destination_relative_path",
}


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

    def __init__(self, config_dir: Path) -> None:
        if not isinstance(config_dir, Path):
            raise TypeError("receipt config directory must be a Path")
        self.root = config_dir.expanduser().resolve() / "resource-portability"
        self.receipt_dir = self.root / "receipts"
        self.pending_dir = self.root / "pending"
        try:
            self.receipt_dir.mkdir(parents=True, exist_ok=True)
            self.pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from error

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
        path = self._pending_path(receipt_template.operation_id)
        if path.exists():
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        self._write_new(path, _pending_to_bytes(pending))
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
        self._replace_pending(ready)
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
        self._replace_pending(manual)
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
        if destination.exists():
            try:
                if receipt_from_bytes(destination.read_bytes()) == receipt:
                    return destination
            except (OSError, ResourcePortabilityError):
                pass
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        self._write_new(destination, payload)
        return destination

    def abandon(self, operation_id: str) -> None:
        path = self._pending_path(operation_id)
        try:
            path.unlink(missing_ok=False)
            _fsync_directory(self.pending_dir)
        except FileNotFoundError:
            return
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from error

    def get(self, operation_id: str) -> ResourceOperationReceipt:
        path = self._receipt_path(operation_id)
        try:
            return receipt_from_bytes(path.read_bytes())
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error

    def list_receipts(self) -> tuple[ResourceOperationReceipt, ...]:
        try:
            paths = sorted(self.receipt_dir.glob("*.json"), key=lambda item: item.name)
            return tuple(receipt_from_bytes(path.read_bytes()) for path in paths)
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error

    def get_pending(self, operation_id: str) -> ResourcePendingOperation:
        path = self._pending_path(operation_id)
        try:
            return _pending_from_bytes(path.read_bytes())
        except FileNotFoundError as error:
            raise ResourcePortabilityError("RESOURCE.IMPORT.PREVIEW_STALE") from error
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error

    def list_pending(self) -> tuple[ResourcePendingOperation, ...]:
        try:
            paths = sorted(self.pending_dir.glob("*.journal"), key=lambda item: item.name)
            return tuple(_pending_from_bytes(path.read_bytes()) for path in paths)
        except OSError as error:
            raise ResourcePortabilityError("RESOURCE.RECEIPT.INVALID") from error

    def _replace_pending(self, pending: ResourcePendingOperation) -> None:
        destination = self._pending_path(pending.receipt.operation_id)
        if not destination.exists():
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        self._atomic_replace(destination, _pending_to_bytes(pending))

    def _write_new(self, destination: Path, payload: bytes) -> None:
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if destination.exists():
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
            os.link(temp, destination)
            temp.unlink()
            temp = None
            _fsync_directory(destination.parent)
            if destination.read_bytes() != payload:
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        except (OSError, ResourcePortabilityError) as error:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if isinstance(error, ResourcePortabilityError):
                raise
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from error

    def _atomic_replace(self, destination: Path, payload: bytes) -> None:
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.stem}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, destination)
            temp = None
            _fsync_directory(destination.parent)
            if destination.read_bytes() != payload:
                raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED")
        except (OSError, ResourcePortabilityError) as error:
            if temp is not None:
                temp.unlink(missing_ok=True)
            if isinstance(error, ResourcePortabilityError):
                raise
            raise ResourcePortabilityError("RESOURCE.RECEIPT.LEDGER_FAILED") from error

    def _receipt_path(self, operation_id: str) -> Path:
        return self.receipt_dir / f"{_safe_operation_id(operation_id)}.json"

    def _pending_path(self, operation_id: str) -> Path:
        return self.pending_dir / f"{_safe_operation_id(operation_id)}.journal"


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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ResourcePendingOperation",
    "ResourcePendingPhase",
    "ResourceReceiptLedger",
]
