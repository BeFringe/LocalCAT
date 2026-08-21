"""Test-only Parser I/O fault injection and authority-state observations.

The objects in this module are deliberately small test doubles.  They provide
repeatable failure checkpoints and isolated files for later Parser contract
tests; they are not a production Source Boundary, atomic writer, Parser, or
resource store implementation.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class FaultPoint(str, Enum):
    """Named seams required by the Wave 0 I/O adversarial harness."""

    SOURCE_CONCURRENT_CHANGE = "source.concurrent_change"
    SOURCE_ROOT_ESCAPE = "source.root_escape"
    SOURCE_NON_REGULAR = "source.non_regular"
    SOURCE_SNAPSHOT_STALE = "source.snapshot_stale"
    WRITER_TEMP_WRITE = "writer.temp_write"
    WRITER_FSYNC = "writer.fsync"
    WRITER_REPLACE = "writer.replace"
    RESOURCE_COMMIT = "resource.commit"


class InjectedIOFault(RuntimeError):
    """Stable failure raised only by the test injector."""

    def __init__(self, point: FaultPoint) -> None:
        self.point = point
        super().__init__(f"injected Parser I/O fault: {point.value}")


class FaultInjector:
    """Record checkpoints and fail at one explicitly selected seam."""

    def __init__(self, fail_at: FaultPoint | None = None) -> None:
        if fail_at is not None and type(fail_at) is not FaultPoint:
            raise TypeError("fail_at must be an exact FaultPoint or None")
        self._fail_at = fail_at
        self._seen: list[FaultPoint] = []

    @property
    def seen(self) -> tuple[FaultPoint, ...]:
        return tuple(self._seen)

    def checkpoint(self, point: FaultPoint) -> None:
        if type(point) is not FaultPoint:
            raise TypeError("checkpoint point must be an exact FaultPoint")
        self._seen.append(point)
        if point is self._fail_at:
            raise InjectedIOFault(point)

    def wrap(self, point: FaultPoint, operation: Callable[..., Any]) -> Callable[..., Any]:
        """Return a patch-friendly wrapper around an arbitrary I/O operation."""

        def wrapped(*args: Any, **kwargs: Any) -> Any:
            self.checkpoint(point)
            return operation(*args, **kwargs)

        return wrapped


@dataclass(frozen=True)
class SourceIdentityProbe:
    """Test observation sufficient to notice stale or concurrent source drift."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True)
class AuthorityState:
    """Authoritative bytes and receipt/commit state around an injected failure."""

    target_bytes: bytes
    resource_bytes: bytes
    write_receipts: tuple[str, ...]
    resource_receipts: tuple[str, ...]
    resource_commit_attempts: int
    resource_commit_successes: int


class ReceiptLedger:
    """Test ledger that keeps receipt issuance distinct from commit attempts."""

    def __init__(self) -> None:
        self.write_receipts: list[str] = []
        self.resource_receipts: list[str] = []
        self.resource_commit_attempts = 0
        self.resource_commit_successes = 0

    def issue_write(self, receipt: str) -> None:
        self.write_receipts.append(receipt)

    def begin_resource_commit(self) -> None:
        self.resource_commit_attempts += 1

    def complete_resource_commit(self, receipt: str) -> None:
        self.resource_commit_successes += 1
        self.resource_receipts.append(receipt)


class ParserIOFaultFixture:
    """Own all source, target, resource, and escape cases under one temp tree."""

    def __init__(self) -> None:
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.receipts = ReceiptLedger()

    def __enter__(self) -> ParserIOFaultFixture:
        if self._temporary is not None:
            raise RuntimeError("ParserIOFaultFixture cannot be entered twice")
        self._temporary = tempfile.TemporaryDirectory(prefix="parser-io-fault-")
        temporary_root = Path(self._temporary.name)
        safe_root = temporary_root / "safe-root"
        outside_root = temporary_root / "outside-root"
        (safe_root / "input").mkdir(parents=True)
        (safe_root / "output").mkdir()
        (safe_root / "non-regular").mkdir()
        outside_root.mkdir()

        (safe_root / "input" / "source.txt").write_bytes(b"source-input")
        (outside_root / "source.txt").write_bytes(b"outside-input")
        (safe_root / "output" / "project.json").write_bytes(b"original-target")
        (safe_root / "output" / "resource.jsonl").write_bytes(b"original-resource")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        temporary = self._require_temporary()
        self._temporary = None
        temporary.cleanup()

    def _require_temporary(self) -> tempfile.TemporaryDirectory[str]:
        if self._temporary is None:
            raise RuntimeError("ParserIOFaultFixture must be used as a context manager")
        return self._temporary

    @property
    def temporary_root(self) -> Path:
        return Path(self._require_temporary().name)

    @property
    def safe_root(self) -> Path:
        return self.temporary_root / "safe-root"

    @property
    def source(self) -> Path:
        return self.safe_root / "input" / "source.txt"

    @property
    def outside_source(self) -> Path:
        return self.temporary_root / "outside-root" / "source.txt"

    @property
    def non_regular_source(self) -> Path:
        return self.safe_root / "non-regular"

    @property
    def target(self) -> Path:
        return self.safe_root / "output" / "project.json"

    @property
    def writer_temp(self) -> Path:
        return self.safe_root / "output" / ".project.json.parser-test-tmp"

    @property
    def resource_target(self) -> Path:
        return self.safe_root / "output" / "resource.jsonl"

    def source_reference(self, point: FaultPoint) -> Path:
        if point is FaultPoint.SOURCE_ROOT_ESCAPE:
            return self.safe_root / ".." / "outside-root" / "source.txt"
        if point is FaultPoint.SOURCE_NON_REGULAR:
            return self.non_regular_source
        raise ValueError(f"{point.value} is not a source-reference fixture")

    def capture_source_identity(self) -> SourceIdentityProbe:
        payload = self.source.read_bytes()
        status = self.source.stat()
        return SourceIdentityProbe(
            device=status.st_dev,
            inode=status.st_ino,
            size=status.st_size,
            mtime_ns=status.st_mtime_ns,
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def mutate_source_concurrently(self, replacement: bytes) -> None:
        self.source.write_bytes(replacement)

    def make_snapshot_stale(self, replacement: bytes) -> None:
        self.source.write_bytes(replacement)

    def snapshot_is_stale(self, expected: SourceIdentityProbe) -> bool:
        return self.capture_source_identity() != expected

    def capture_authority_state(self) -> AuthorityState:
        return AuthorityState(
            target_bytes=self.target.read_bytes(),
            resource_bytes=self.resource_target.read_bytes(),
            write_receipts=tuple(self.receipts.write_receipts),
            resource_receipts=tuple(self.receipts.resource_receipts),
            resource_commit_attempts=self.receipts.resource_commit_attempts,
            resource_commit_successes=self.receipts.resource_commit_successes,
        )

    def assert_failed_preserving_authority(
        self,
        before: AuthorityState,
        after: AuthorityState,
        *,
        expected_resource_attempt_delta: int = 0,
    ) -> None:
        if after.target_bytes != before.target_bytes:
            raise AssertionError("target bytes changed after failed operation")
        if after.resource_bytes != before.resource_bytes:
            raise AssertionError("resource bytes changed after failed operation")
        if after.write_receipts != before.write_receipts:
            raise AssertionError("write receipt changed after failed operation")
        if after.resource_receipts != before.resource_receipts:
            raise AssertionError("resource receipt changed after failed operation")
        if after.resource_commit_successes != before.resource_commit_successes:
            raise AssertionError("resource commit completed after failed operation")
        expected_attempts = before.resource_commit_attempts + expected_resource_attempt_delta
        if after.resource_commit_attempts != expected_attempts:
            raise AssertionError(
                "unexpected resource commit attempt count after failed operation"
            )


class FaultingFilesystemOps:
    """Patch-friendly filesystem operation double for later writer tests."""

    def __init__(self, injector: FaultInjector) -> None:
        self._injector = injector

    def write_temp(self, path: Path, payload: bytes) -> None:
        self._injector.checkpoint(FaultPoint.WRITER_TEMP_WRITE)
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()

    def fsync(self, path: Path) -> None:
        self._injector.checkpoint(FaultPoint.WRITER_FSYNC)
        with path.open("rb") as handle:
            os.fsync(handle.fileno())

    def replace(self, source: Path, target: Path) -> None:
        self._injector.checkpoint(FaultPoint.WRITER_REPLACE)
        os.replace(source, target)


class FaultingResourceCommitPort:
    """Resource transaction test double with explicit attempt/success state."""

    def __init__(
        self,
        target: Path,
        receipts: ReceiptLedger,
        injector: FaultInjector,
    ) -> None:
        self._target = target
        self._receipts = receipts
        self._injector = injector

    def commit(self, payload: bytes) -> str:
        self._receipts.begin_resource_commit()
        self._injector.checkpoint(FaultPoint.RESOURCE_COMMIT)
        self._target.write_bytes(payload)
        receipt = f"resource-receipt-{self._receipts.resource_commit_successes + 1}"
        self._receipts.complete_resource_commit(receipt)
        return receipt
