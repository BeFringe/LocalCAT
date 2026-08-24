"""Direct ``.tmx`` publication with bound before-fact and verified recovery."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import secrets
import stat
from typing import Callable

from tmx_context_contracts import (
    TmxContextError,
    TmxDestinationBeforeKind,
    TmxDirectPlan,
    TmxDirectReceipt,
    TmxExportPreview,
    TmxPayloadProof,
    TmxPreparedPayload,
    TmxScopeBinding,
)


@dataclass(frozen=True, slots=True)
class _RegularFact:
    device: int
    inode: int
    size: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True, slots=True)
class _DestinationFact:
    parent: Path
    parent_device: int
    parent_inode: int
    name: str
    before_kind: TmxDestinationBeforeKind
    regular: _RegularFact | None


def _digest_fd(fd: int) -> str:
    digest = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, 64 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _open_parent(parent: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(parent, flags)
    except OSError as exc:
        raise TmxContextError("TMX.DESTINATION_PARENT_INVALID", "destination parent is not a stable directory") from exc


def _regular_fact(parent_fd: int, name: str) -> _RegularFact | None:
    try:
        observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TmxContextError("TMX.DESTINATION_INSPECTION_FAILED", "destination could not be inspected") from exc
    if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
        raise TmxContextError(
            "TMX.DESTINATION_UNSAFE",
            "destination must be an absent or single-link regular file",
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise TmxContextError("TMX.DESTINATION_INSPECTION_FAILED", "destination could not be opened safely") from exc
    try:
        pinned = os.fstat(fd)
        if (pinned.st_dev, pinned.st_ino) != (observed.st_dev, observed.st_ino):
            raise TmxContextError("TMX.DESTINATION_STALE", "destination changed during inspection")
        return _RegularFact(
            device=pinned.st_dev,
            inode=pinned.st_ino,
            size=pinned.st_size,
            mtime_ns=pinned.st_mtime_ns,
            digest=_digest_fd(fd),
        )
    finally:
        os.close(fd)


def _same_regular(left: _RegularFact | None, right: _RegularFact | None) -> bool:
    return left == right


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(fd, view[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        offset += written


class TmxDirectArtifactSaver:
    """Issue private plans and publish only after owner and Parser revalidation."""

    __slots__ = ("_cold_validator", "_scope_revalidator", "_issued")

    def __init__(
        self,
        cold_validator: Callable[[Path, TmxPayloadProof], None],
        scope_revalidator: Callable[[TmxScopeBinding], None],
    ) -> None:
        if not callable(cold_validator) or not callable(scope_revalidator):
            raise TypeError("TMX validators must be callable")
        self._cold_validator = cold_validator
        self._scope_revalidator = scope_revalidator
        self._issued: dict[str, TmxDirectPlan] = {}

    def preview(
        self,
        binding: TmxScopeBinding,
        payload: TmxPreparedPayload,
        destination: Path,
    ) -> tuple[TmxExportPreview, TmxDirectPlan]:
        if type(binding) is not TmxScopeBinding:
            raise TypeError("binding must be exact TmxScopeBinding")
        if type(payload) is not TmxPreparedPayload:
            raise TypeError("payload must be exact TmxPreparedPayload")
        if (
            payload.scope_kind is not binding.scope_kind
            or payload.scope_id != binding.scope_id
            or payload.binding_digest != binding.binding_digest
        ):
            raise TmxContextError("TMX.SCOPE_PAYLOAD_MISMATCH", "TMX payload was prepared for a different scope")
        if not isinstance(destination, Path) or not destination.is_absolute():
            raise TypeError("destination must be an absolute Path")
        if destination.suffix.casefold() != ".tmx" or destination.name in ("", ".", ".."):
            raise TmxContextError("TMX.DESTINATION_EXTENSION", "direct TMX destination must end in .tmx")
        parent = destination.parent
        parent_fd = _open_parent(parent)
        try:
            parent_stat = os.fstat(parent_fd)
            regular = _regular_fact(parent_fd, destination.name)
        finally:
            os.close(parent_fd)
        before_kind = (
            TmxDestinationBeforeKind.ABSENT
            if regular is None
            else TmxDestinationBeforeKind.REGULAR
        )
        operation_id = secrets.token_hex(16)
        loss = payload.proof.loss_report
        preview = TmxExportPreview(
            operation_id=operation_id,
            scope_kind=binding.scope_kind,
            scope_id=binding.scope_id,
            project_id=binding.project_id,
            chunk_plan_id=binding.chunk_plan_id,
            chunk_plan_revision=binding.chunk_plan_revision,
            chunk_id=binding.chunk_id,
            document_count=binding.document_count,
            attached_count=binding.attached_count,
            included_count=loss.included_count,
            excluded_count=loss.excluded_count,
            warning_count=loss.warning_count,
            loss_counts=loss.counts,
            safe_issues=loss.issues,
            effective_locales=payload.proof.effective_locales,
            profile_id=payload.proof.profile_id,
            destination=destination,
            destination_before=before_kind,
            destination_before_digest=regular.digest if regular else None,
        )
        fact = _DestinationFact(
            parent=parent,
            parent_device=parent_stat.st_dev,
            parent_inode=parent_stat.st_ino,
            name=destination.name,
            before_kind=before_kind,
            regular=regular,
        )
        plan = TmxDirectPlan(
            operation_id=operation_id,
            payload=payload,
            binding=binding,
            preview=preview,
            destination_fact=fact,
        )
        self._issued[operation_id] = plan
        return preview, plan

    def cancel(self, plan: TmxDirectPlan) -> None:
        self._claim(plan)
        plan._consume()

    def _claim(self, plan: TmxDirectPlan) -> None:
        if type(plan) is not TmxDirectPlan:
            raise TypeError("plan must be exact TmxDirectPlan")
        operation_id = plan._operation_id
        if self._issued.pop(operation_id, None) is not plan:
            raise TmxContextError("TMX.PLAN_INVALID", "direct export plan is not active")

    def apply(self, plan: TmxDirectPlan) -> TmxDirectReceipt:
        self._claim(plan)
        payload, binding, preview, raw_fact = plan._consume()
        if type(raw_fact) is not _DestinationFact:
            raise TmxContextError("TMX.PLAN_INVALID", "direct export plan has invalid destination facts")
        fact = raw_fact

        try:
            revalidated = self._scope_revalidator(binding)
            if revalidated is False or (
                type(revalidated) is TmxScopeBinding and revalidated != binding
            ):
                raise TmxContextError("TMX.SCOPE_STALE", "source scope no longer matches preview")
            if revalidated not in (None, True) and type(revalidated) is not TmxScopeBinding:
                raise TypeError("scope revalidator must return None, True, or exact TmxScopeBinding")
        except TmxContextError:
            raise
        except Exception as exc:
            raise TmxContextError("TMX.SCOPE_REVALIDATION_FAILED", "source scope no longer matches preview") from exc

        parent_fd = _open_parent(fact.parent)
        candidate_name = f".localcat-tmx-{preview.operation_id}.candidate"
        lkg_name = f".localcat-tmx-{preview.operation_id}.lkg"
        candidate_created = False
        lkg_created = False
        published = False
        candidate_fact: _RegularFact | None = None
        try:
            parent_now = os.fstat(parent_fd)
            if (parent_now.st_dev, parent_now.st_ino) != (fact.parent_device, fact.parent_inode):
                raise TmxContextError("TMX.DESTINATION_PARENT_STALE", "destination parent changed after preview")
            if not _same_regular(_regular_fact(parent_fd, fact.name), fact.regular):
                raise TmxContextError("TMX.DESTINATION_STALE", "destination changed after preview")

            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                candidate_fd = os.open(candidate_name, flags, 0o600, dir_fd=parent_fd)
            except OSError as exc:
                raise TmxContextError("TMX.CANDIDATE_CREATE_FAILED", "exclusive TMX candidate could not be created") from exc
            candidate_created = True
            try:
                _write_all(candidate_fd, payload.data)
                os.fsync(candidate_fd)
            except OSError as exc:
                raise TmxContextError("TMX.CANDIDATE_WRITE_FAILED", "TMX candidate could not be durably written") from exc
            finally:
                os.close(candidate_fd)

            candidate_path = fact.parent / candidate_name
            self._cold_validator(candidate_path, payload.proof)
            candidate_fact = _regular_fact(parent_fd, candidate_name)
            if candidate_fact is None or candidate_fact.digest != payload.proof.payload_digest:
                raise TmxContextError("TMX.CANDIDATE_STALE", "TMX candidate changed after cold validation")
            if not _same_regular(_regular_fact(parent_fd, fact.name), fact.regular):
                raise TmxContextError("TMX.DESTINATION_STALE", "destination changed before publication")

            if fact.regular is not None:
                source_fd = os.open(
                    fact.name,
                    os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=parent_fd,
                )
                try:
                    source_stat = os.fstat(source_fd)
                    if (
                        source_stat.st_dev,
                        source_stat.st_ino,
                        source_stat.st_size,
                        source_stat.st_mtime_ns,
                    ) != (
                        fact.regular.device,
                        fact.regular.inode,
                        fact.regular.size,
                        fact.regular.mtime_ns,
                    ):
                        raise TmxContextError("TMX.DESTINATION_STALE", "destination changed before LKG capture")
                    lkg_fd = os.open(lkg_name, flags, 0o600, dir_fd=parent_fd)
                    lkg_created = True
                    try:
                        os.lseek(source_fd, 0, os.SEEK_SET)
                        while True:
                            chunk = os.read(source_fd, 64 * 1024)
                            if not chunk:
                                break
                            _write_all(lkg_fd, chunk)
                        os.fsync(lkg_fd)
                    finally:
                        os.close(lkg_fd)
                finally:
                    os.close(source_fd)
                lkg_fact = _regular_fact(parent_fd, lkg_name)
                if lkg_fact is None or lkg_fact.digest != fact.regular.digest:
                    raise TmxContextError("TMX.LKG_FAILED", "prior destination could not be verified")

            if not _same_regular(_regular_fact(parent_fd, fact.name), fact.regular):
                raise TmxContextError("TMX.DESTINATION_STALE", "destination changed immediately before publication")
            if fact.regular is None:
                try:
                    os.link(
                        candidate_name,
                        fact.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise TmxContextError("TMX.DESTINATION_STALE", "absent destination appeared before publication") from exc
                try:
                    os.unlink(candidate_name, dir_fd=parent_fd)
                    candidate_created = False
                    published = True
                except OSError as exc:
                    try:
                        os.unlink(fact.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except OSError as recovery_exc:
                        raise TmxContextError("TMX.RECOVERY_REQUIRED", "absent publication could not be recovered") from recovery_exc
                    raise TmxContextError("TMX.PUBLICATION_FAILED", "absent TMX publication was rolled back") from exc
            else:
                os.replace(candidate_name, fact.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                candidate_created = False
                published = True
            os.fsync(parent_fd)

            try:
                try:
                    current = _regular_fact(parent_fd, fact.name)
                except TmxContextError as unsafe_exc:
                    raise TmxContextError(
                        "TMX.RECOVERY_REQUIRED",
                        "published destination became unsafe before verified recovery",
                    ) from unsafe_exc
                if current is None or candidate_fact is None:
                    raise TmxContextError("TMX.READBACK_FAILED", "published TMX is missing")
                if (current.device, current.inode, current.digest) != (
                    candidate_fact.device,
                    candidate_fact.inode,
                    candidate_fact.digest,
                ):
                    raise TmxContextError("TMX.READBACK_FAILED", "published TMX no longer matches the candidate")
                self._cold_validator(fact.parent / fact.name, payload.proof)
            except Exception as exc:
                current = _regular_fact(parent_fd, fact.name)
                recoverable = (
                    current is not None
                    and candidate_fact is not None
                    and (current.device, current.inode, current.digest)
                    == (candidate_fact.device, candidate_fact.inode, candidate_fact.digest)
                )
                if not recoverable:
                    raise TmxContextError(
                        "TMX.RECOVERY_REQUIRED",
                        "published destination changed before verified recovery",
                    ) from exc
                try:
                    if fact.regular is None:
                        os.unlink(fact.name, dir_fd=parent_fd)
                    else:
                        os.replace(lkg_name, fact.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        lkg_created = False
                    os.fsync(parent_fd)
                except OSError as recovery_exc:
                    raise TmxContextError("TMX.RECOVERY_REQUIRED", "published destination could not be recovered") from recovery_exc
                published = False
                raise TmxContextError(
                    "TMX.POST_PUBLICATION_ROLLED_BACK",
                    "published TMX failed readback and prior state was restored",
                ) from exc

            if lkg_created:
                os.unlink(lkg_name, dir_fd=parent_fd)
                lkg_created = False
            loss = payload.proof.loss_report
            return TmxDirectReceipt(
                operation_id=preview.operation_id,
                scope_kind=binding.scope_kind,
                scope_id=binding.scope_id,
                profile_id=payload.proof.profile_id,
                effective_locales=payload.proof.effective_locales,
                destination=preview.destination,
                destination_before=preview.destination_before,
                before_digest=preview.destination_before_digest,
                after_digest=payload.proof.payload_digest,
                included_count=loss.included_count,
                excluded_count=loss.excluded_count,
                warning_count=loss.warning_count,
                loss_counts=loss.counts,
                durable=True,
            )
        except TmxContextError:
            raise
        except OSError as exc:
            if published:
                raise TmxContextError("TMX.RECOVERY_REQUIRED", "publication durability could not be established") from exc
            raise TmxContextError("TMX.PUBLICATION_FAILED", "TMX destination was not published") from exc
        finally:
            if candidate_created:
                try:
                    os.unlink(candidate_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            if lkg_created and not published:
                try:
                    os.unlink(lkg_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
            os.close(parent_fd)
