"""Application-owned explicit selected-files intake for project workspaces.

Only the caller-provided tuple is opened.  The module retains one root dirfd for
the whole batch, pre-binds every selected regular file without following links,
and then delegates all format grammar and terminal proof to the Parser surface.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import stat

from parser_composition import create_parser_application_surface
from parser_contracts import (
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    FormatId,
    GETTEXT_PO_V1,
    GETTEXT_POT_V1,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    MetadataEntry,
    ParsedSegment,
    ReadRequest,
    SelectionFailure,
    SelectionRequest,
    SourceReference,
    SourceSnapshotIdentity,
    TargetPresence,
    TerminalSuccess,
    TranslationState,
)
from project_workspace_contracts import (
    MAX_PROJECT_DOCUMENTS,
    EditingOverlayEntry,
    OriginBinding,
    OriginBindingDocument,
    ProjectDocument,
    ProjectOrigin,
    ProjectOriginKind,
    ProjectPersistenceKind,
    ProjectSourceSegment,
    ProjectWorkspace,
    ProjectWorkspaceError,
    StagedSelectedProjectDocuments,
    WriterCapabilitySnapshot,
)
from project_workspace_identity import (
    derive_explicit_selected_document_id,
    editing_state_digest_v1,
    issue_project_id,
    normalize_portable_ref_v1,
    source_fingerprint_v1,
    validate_document_id,
    validate_portable_ref_collection,
)


_PROFILE = "explicit-selected-files-v1"
_COPY_BYTES = 1024 * 1024
_FORMAT_BY_SUFFIX = {
    ".json": LOCALCAT_JSON_V1,
    ".txt": LINE_TEXT_V1,
    ".po": GETTEXT_PO_V1,
    ".pot": GETTEXT_POT_V1,
}


def _fail(code: str) -> None:
    raise ProjectWorkspaceError(code)


def _text(value: object, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    assert type(value) is str
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    return value


@dataclass(frozen=True, slots=True)
class OriginRenameMapping:
    old_source_ref: str
    new_source_ref: str
    document_id: str

    def __post_init__(self) -> None:
        old = normalize_portable_ref_v1(self.old_source_ref)
        new = normalize_portable_ref_v1(self.new_source_ref)
        if old != self.old_source_ref or new != self.new_source_ref or old == new:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        validate_document_id(self.document_id)


@dataclass(frozen=True, slots=True)
class SelectedProjectDocumentsRequest:
    name: str
    source_locale: str
    target_locale: str
    origin_binding: OriginBinding | None = None
    expected_binding_revision: int | None = None
    rename_mappings: tuple[OriginRenameMapping, ...] = ()

    def __post_init__(self) -> None:
        _text(self.name)
        _text(self.source_locale)
        _text(self.target_locale)
        if type(self.rename_mappings) is not tuple:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if any(type(item) is not OriginRenameMapping for item in self.rename_mappings):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        if self.origin_binding is None:
            if self.expected_binding_revision is not None or self.rename_mappings:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
        else:
            if type(self.origin_binding) is not OriginBinding:
                _fail("PROJECT.RECONCILE.INPUT_INVALID")
            if (
                type(self.expected_binding_revision) is not int
                or self.expected_binding_revision < 1
            ):
                _fail("PROJECT.RECONCILE.INPUT_INVALID")


@dataclass(slots=True)
class _BoundSelectedFile:
    source_ref: str
    selected_path: Path
    descriptor: int
    initial_status: os.stat_result

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor = self.descriptor
            self.descriptor = -1
            os.close(descriptor)


def _open_flags(*, directory: bool) -> int:
    if not all(
        hasattr(os, name)
        for name in ("O_NOFOLLOW", "O_CLOEXEC")
    ) or not hasattr(os, "O_DIRECTORY"):
        _fail("PROJECT.INTAKE.SOURCE_UNSAFE")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    if directory:
        flags |= os.O_DIRECTORY
    elif hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    return flags


def _absolute_root(root: Path) -> Path:
    if not isinstance(root, Path) or not root.is_absolute():
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    if "\x00" in os.fspath(root):
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    return Path(os.path.abspath(os.fspath(root)))


def _selected_ref(root: Path, selected: Path) -> tuple[str, Path]:
    if not isinstance(selected, Path):
        raise TypeError("selected paths must contain pathlib.Path values")
    path = selected if selected.is_absolute() else root / selected
    try:
        relative = path.relative_to(root)
    except ValueError:
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    parts = relative.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        _fail("PROJECT.WORKSPACE.PATH_INVALID")
    source_ref = normalize_portable_ref_v1("/".join(parts))
    return source_ref, root.joinpath(*source_ref.split("/"))


def _open_below_root(root_descriptor: int, source_ref: str) -> tuple[int, os.stat_result]:
    current = os.dup(root_descriptor)
    try:
        parts = source_ref.split("/")
        for component in parts[:-1]:
            child = os.open(
                component,
                _open_flags(directory=True),
                dir_fd=current,
            )
            os.close(current)
            current = child
        descriptor = os.open(
            parts[-1],
            _open_flags(directory=False),
            dir_fd=current,
        )
    except OSError as error:
        _fail("PROJECT.INTAKE.SOURCE_UNSAFE")
    finally:
        os.close(current)
    try:
        status = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        _fail("PROJECT.INTAKE.SOURCE_UNSAFE")
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        os.close(descriptor)
        _fail("PROJECT.INTAKE.SOURCE_UNSAFE")
    return descriptor, status


def _status_key(status: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_nlink,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _root_identity(root: Path) -> tuple[int, int, int]:
    try:
        descriptor = os.open(root, _open_flags(directory=True))
    except OSError:
        _fail("PROJECT.INTAKE.SOURCE_UNSAFE")
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            _fail("PROJECT.INTAKE.SOURCE_UNSAFE")
        return descriptor, status.st_dev, status.st_ino
    except BaseException:
        os.close(descriptor)
        raise


def _revalidate_root(root: Path, root_device: int, root_inode: int) -> None:
    descriptor, device, inode = _root_identity(root)
    os.close(descriptor)
    if (device, inode) != (root_device, root_inode):
        _fail("PROJECT.INTAKE.SOURCE_STALE")


def _descriptor_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        try:
            block = os.pread(descriptor, _COPY_BYTES, offset)
        except OSError:
            _fail("PROJECT.INTAKE.SOURCE_STALE")
        if not block:
            return digest.hexdigest()
        digest.update(block)
        offset += len(block)


def _metadata_value_bytes(value: object) -> bytes:
    if value is None:
        return b"n"
    if type(value) is bool:
        return b"b1" if value else b"b0"
    if type(value) is int:
        return b"i" + str(value).encode("ascii") + b";"
    if type(value) is float:
        if not math.isfinite(value):
            _fail("PROJECT.INTAKE.INPUT_INVALID")
        return b"f" + value.hex().encode("ascii") + b";"
    if type(value) is str:
        payload = value.encode("utf-8", errors="strict")
        return b"s" + len(payload).to_bytes(8, "big") + payload
    if type(value) is tuple:
        return (
            b"t"
            + len(value).to_bytes(8, "big")
            + b"".join(_metadata_value_bytes(item) for item in value)
        )
    _fail("PROJECT.INTAKE.INPUT_INVALID")


def _codec_source_state_digest(entries: tuple[MetadataEntry, ...]) -> str:
    if type(entries) is not tuple or any(type(item) is not MetadataEntry for item in entries):
        _fail("PROJECT.INTAKE.INPUT_INVALID")
    digest = hashlib.sha256(b"localcat.codec-source-state.v1\0")
    for entry in entries:
        key = entry.key.encode("utf-8", errors="strict")
        value = _metadata_value_bytes(entry.value)
        digest.update(len(key).to_bytes(8, "big"))
        digest.update(key)
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    return digest.hexdigest()


def _confirmed(segment: ParsedSegment) -> bool:
    return segment.translation_state is TranslationState.CONFIRMED


def _target(segment: ParsedSegment) -> str:
    if segment.target_presence is TargetPresence.MISSING:
        return ""
    if segment.target is None:
        _fail("PROJECT.INTAKE.INPUT_INVALID")
    return segment.target


def _document_from_materialized(
    *,
    document_id: str,
    source_ref: str,
    order: int,
    format_id: FormatId,
    opened,
    materialized,
) -> ProjectDocument:
    descriptor = opened.descriptor
    source_identity = opened.source_identity
    terminal = materialized.terminal
    if type(terminal) is not TerminalSuccess:
        _fail("PROJECT.INTAKE.INPUT_INVALID")
    if (
        descriptor.purpose is not EffectivePurpose.PROJECT_DOCUMENT
        or descriptor.format_id != format_id
        or terminal.source != source_identity
        or terminal.codec_identity != descriptor.identity
        or terminal.limit_profile != descriptor.limit_profile
        or terminal.record_count != len(materialized.records)
    ):
        _fail("PROJECT.INTAKE.INPUT_INVALID")
    if type(materialized.header) is not DocumentHeader:
        _fail("PROJECT.INTAKE.INPUT_INVALID")
    if any(type(record) is not ParsedSegment for record in materialized.records):
        _fail("PROJECT.INTAKE.INPUT_INVALID")
    source_segments: list[ProjectSourceSegment] = []
    overlays: list[EditingOverlayEntry] = []
    for record in materialized.records:
        assert type(record) is ParsedSegment
        state_digest = _codec_source_state_digest(record.format_metadata)
        fingerprint = source_fingerprint_v1(
            record.source,
            record.speaker.value,
            state_digest,
        )
        target = _target(record)
        confirmed = _confirmed(record)
        source_segments.append(
            ProjectSourceSegment(
                local_segment_id=record.local_id,
                source=record.source,
                raw_speaker=record.speaker.value,
                source_fingerprint=fingerprint,
            )
        )
        overlays.append(
            EditingOverlayEntry(
                document_id=document_id,
                local_segment_id=record.local_id,
                source_fingerprint=fingerprint,
                target=target,
                confirmed=confirmed,
                saved_state_digest=editing_state_digest_v1(
                    document_id,
                    record.local_id,
                    fingerprint,
                    target,
                    confirmed,
                ),
            )
        )
    capabilities = descriptor.capabilities
    return ProjectDocument(
        document_id=document_id,
        source_ref=source_ref,
        display_name=materialized.header.name,
        order=order,
        format_id=format_id.value,
        codec_identity=descriptor.identity,
        writer_capability_snapshot=WriterCapabilitySnapshot(
            canonical_write=capabilities.canonical_write,
            source_round_trip_write=capabilities.source_round_trip_write,
            format_profile=capabilities.format_profile,
        ),
        source_snapshot_digest=source_identity.content_sha256,
        source_segments=tuple(source_segments),
        editing_overlay=tuple(overlays),
        codec_private_member=None,
    )


def _validate_existing_binding(
    request: SelectedProjectDocumentsRequest,
    *,
    root: Path,
    root_device: int,
    root_inode: int,
    source_refs: tuple[str, ...],
) -> tuple[str, dict[str, str], int]:
    binding = request.origin_binding
    if binding is None:
        return issue_project_id(), {}, 1
    if (
        request.expected_binding_revision != binding.revision
        or binding.absolute_root != str(root)
        or (binding.root_device, binding.root_inode) != (root_device, root_inode)
    ):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    by_ref = {item.source_ref: item.document_id for item in binding.documents}
    by_id = {item.document_id: item.source_ref for item in binding.documents}
    old_refs: set[str] = set()
    new_refs: set[str] = set()
    renamed: dict[str, str] = {}
    for mapping in request.rename_mappings:
        if (
            mapping.old_source_ref in old_refs
            or mapping.new_source_ref in new_refs
            or by_ref.get(mapping.old_source_ref) != mapping.document_id
            or by_id.get(mapping.document_id) != mapping.old_source_ref
            or mapping.old_source_ref in source_refs
            or mapping.new_source_ref not in source_refs
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        old_refs.add(mapping.old_source_ref)
        new_refs.add(mapping.new_source_ref)
        renamed[mapping.new_source_ref] = mapping.document_id
    assigned = {
        source_ref: by_ref.get(
            source_ref,
            renamed.get(source_ref, derive_explicit_selected_document_id(source_ref)),
        )
        for source_ref in source_refs
    }
    if len(set(assigned.values())) != len(assigned):
        _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")
    previous_mapping = frozenset(
        (item.source_ref, item.document_id) for item in binding.documents
    )
    next_mapping = frozenset(assigned.items())
    revision = binding.revision + (previous_mapping != next_mapping)
    return binding.project_id, assigned, revision


def _issue_staged(
    workspace: ProjectWorkspace,
    origin_binding: OriginBinding,
    source_identities: tuple[SourceSnapshotIdentity, ...],
) -> StagedSelectedProjectDocuments:
    return StagedSelectedProjectDocuments(
        workspace=workspace,
        origin_binding=origin_binding,
        source_identities=source_identities,
        source_write_back_authorized=False,
        durable=False,
    )


def stage_selected_project_documents(
    root: Path,
    selected_paths: tuple[Path, ...],
    request: SelectedProjectDocumentsRequest,
) -> StagedSelectedProjectDocuments:
    """Stage exactly the selected project documents without scanning the root."""

    if type(selected_paths) is not tuple:
        raise TypeError("selected_paths must be an exact tuple")
    if len(selected_paths) < 2:
        _fail("PROJECT.WORKSPACE.CONTRACT_INVALID")
    if len(selected_paths) > MAX_PROJECT_DOCUMENTS:
        _fail("PROJECT.WORKSPACE.LIMIT_EXCEEDED")
    if type(request) is not SelectedProjectDocumentsRequest:
        raise TypeError("request must be exact SelectedProjectDocumentsRequest")
    root_path = _absolute_root(root)
    selected = tuple(_selected_ref(root_path, path) for path in selected_paths)
    source_refs = tuple(item[0] for item in selected)
    validate_portable_ref_collection(source_refs, allow_exact_duplicates=False)
    formats: list[FormatId] = []
    for source_ref in source_refs:
        format_id = _FORMAT_BY_SUFFIX.get(Path(source_ref).suffix.lower())
        if format_id is None:
            _fail("PROJECT.INTAKE.INPUT_INVALID")
        formats.append(format_id)

    root_descriptor, root_device, root_inode = _root_identity(root_path)
    bound: list[_BoundSelectedFile] = []
    try:
        project_id, assigned_ids, binding_revision = _validate_existing_binding(
            request,
            root=root_path,
            root_device=root_device,
            root_inode=root_inode,
            source_refs=source_refs,
        )
        observed_file_ids: set[tuple[int, int]] = set()
        for source_ref, selected_path in selected:
            descriptor, status = _open_below_root(root_descriptor, source_ref)
            file_id = (status.st_dev, status.st_ino)
            if file_id in observed_file_ids:
                os.close(descriptor)
                _fail("PROJECT.WORKSPACE.IDENTITY_DUPLICATE")
            observed_file_ids.add(file_id)
            bound.append(
                _BoundSelectedFile(source_ref, selected_path, descriptor, status)
            )

        if request.origin_binding is not None and request.rename_mappings:
            previous_by_ref = {
                item.source_ref: item for item in request.origin_binding.documents
            }
            rename_by_new_ref = {
                item.new_source_ref: item for item in request.rename_mappings
            }
            for bound_file in bound:
                mapping = rename_by_new_ref.get(bound_file.source_ref)
                if mapping is None:
                    continue
                previous = previous_by_ref[mapping.old_source_ref]
                current_regular_identity = (
                    f"{bound_file.initial_status.st_dev}:"
                    f"{bound_file.initial_status.st_ino}"
                )
                if (
                    previous.document_id != mapping.document_id
                    or previous.source_identity.regular_file_identity
                    != current_regular_identity
                ):
                    _fail("PROJECT.RECONCILE.INPUT_INVALID")

        surface = create_parser_application_surface()
        documents: list[ProjectDocument] = []
        source_identities: list[SourceSnapshotIdentity] = []
        binding_documents: list[OriginBindingDocument] = []
        for order, (bound_file, format_id) in enumerate(
            zip(bound, formats, strict=True)
        ):
            purpose = EffectivePurpose.PROJECT_DOCUMENT
            try:
                opened = surface.open_input(
                    SourceReference(
                        safe_root=str(root_path),
                        selected_path=str(bound_file.selected_path),
                        display_hint=Path(bound_file.source_ref).name,
                    ),
                    SelectionRequest(purpose=purpose, format_id=format_id),
                    ReadRequest(purpose=purpose, format_id=format_id),
                )
                if type(opened) is SelectionFailure:
                    _fail("PROJECT.INTAKE.INPUT_INVALID")
                assert not isinstance(opened, SelectionFailure)
                with opened:
                    materialized = opened.materialize()
                    source_identity = opened.source_identity
                    document_id = assigned_ids.get(
                        bound_file.source_ref,
                        derive_explicit_selected_document_id(bound_file.source_ref),
                    )
                    document = _document_from_materialized(
                        document_id=document_id,
                        source_ref=bound_file.source_ref,
                        order=order,
                        format_id=format_id,
                        opened=opened,
                        materialized=materialized,
                    )
            except ProjectWorkspaceError:
                raise
            except ContractViolation as error:
                raise ProjectWorkspaceError("PROJECT.INTAKE.INPUT_INVALID") from error
            expected = bound_file.initial_status
            if (
                source_identity.relative_reference_sha256
                != hashlib.sha256(
                    bound_file.source_ref.encode("utf-8", errors="strict")
                ).hexdigest()
                or
                source_identity.regular_file_identity
                != f"{expected.st_dev}:{expected.st_ino}"
                or source_identity.original_size != expected.st_size
                or source_identity.original_mtime_ns != expected.st_mtime_ns
            ):
                _fail("PROJECT.INTAKE.SOURCE_STALE")
            documents.append(document)
            source_identities.append(source_identity)
            binding_documents.append(
                OriginBindingDocument(
                    source_ref=bound_file.source_ref,
                    document_id=document.document_id,
                    format_id=document.format_id,
                    codec_identity=document.codec_identity,
                    source_identity=source_identity,
                )
            )

        _revalidate_root(root_path, root_device, root_inode)
        for bound_file, identity in zip(bound, source_identities, strict=True):
            try:
                final_status = os.fstat(bound_file.descriptor)
            except OSError:
                _fail("PROJECT.INTAKE.SOURCE_STALE")
            if (
                _status_key(final_status) != _status_key(bound_file.initial_status)
                or _descriptor_digest(bound_file.descriptor) != identity.content_sha256
            ):
                _fail("PROJECT.INTAKE.SOURCE_STALE")
        retained_root_status = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(retained_root_status.st_mode)
            or (retained_root_status.st_dev, retained_root_status.st_ino)
            != (root_device, root_inode)
        ):
            _fail("PROJECT.INTAKE.SOURCE_STALE")

        workspace = ProjectWorkspace(
            schema_version=1,
            project_id=project_id,
            name=request.name,
            source_locale=request.source_locale,
            target_locale=request.target_locale,
            origin=ProjectOrigin(
                kind=ProjectOriginKind.DIRECTORY,
                profile_version=_PROFILE,
                portable_root_ref="project",
            ),
            persistence_kind=ProjectPersistenceKind.PROJECT_PACKAGE,
            documents=tuple(documents),
        )
        binding = OriginBinding(
            schema_version=1,
            project_id=project_id,
            profile_version=_PROFILE,
            absolute_root=str(root_path),
            root_device=root_device,
            root_inode=root_inode,
            revision=binding_revision,
            documents=tuple(binding_documents),
        )
        return _issue_staged(workspace, binding, tuple(source_identities))
    except ProjectWorkspaceError:
        raise
    except OSError as error:
        raise ProjectWorkspaceError("PROJECT.INTAKE.SOURCE_UNSAFE") from error
    finally:
        for item in bound:
            item.close()
        os.close(root_descriptor)


def _workspace_rebind_document_ids(
    root: Path,
    selected_paths: tuple[Path, ...],
    workspace: ProjectWorkspace,
    rename_mappings: tuple[OriginRenameMapping, ...],
) -> tuple[tuple[str, str], ...]:
    """Resolve every explicit selection to exactly one cold manifest identity."""

    if type(selected_paths) is not tuple:
        raise TypeError("selected_paths must be an exact tuple")
    if type(workspace) is not ProjectWorkspace:
        raise TypeError("workspace must be an exact ProjectWorkspace")
    if workspace.persistence_kind is not ProjectPersistenceKind.PROJECT_PACKAGE:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    if type(rename_mappings) is not tuple:
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    if any(type(item) is not OriginRenameMapping for item in rename_mappings):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")

    root_path = _absolute_root(root)
    selected_refs = tuple(
        _selected_ref(root_path, selected_path)[0]
        for selected_path in selected_paths
    )
    validate_portable_ref_collection(selected_refs, allow_exact_duplicates=False)

    manifest_by_ref = {
        document.source_ref: document
        for document in workspace.documents
    }
    manifest_by_id = {
        document.document_id: document
        for document in workspace.documents
    }
    if len(manifest_by_ref) != len(workspace.documents):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    selected_ref_set = set(selected_refs)
    old_refs: set[str] = set()
    new_refs: set[str] = set()
    mapped_document_ids: set[str] = set()
    rename_by_new_ref: dict[str, str] = {}
    for mapping in rename_mappings:
        manifest_document = manifest_by_ref.get(mapping.old_source_ref)
        if (
            manifest_document is None
            or manifest_document.document_id != mapping.document_id
            or manifest_by_id.get(mapping.document_id) is not manifest_document
            or mapping.old_source_ref in selected_ref_set
            or mapping.new_source_ref not in selected_ref_set
            or mapping.new_source_ref in manifest_by_ref
            or mapping.old_source_ref in old_refs
            or mapping.new_source_ref in new_refs
            or mapping.document_id in mapped_document_ids
        ):
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        old_refs.add(mapping.old_source_ref)
        new_refs.add(mapping.new_source_ref)
        mapped_document_ids.add(mapping.document_id)
        rename_by_new_ref[mapping.new_source_ref] = mapping.document_id

    assigned: list[str] = []
    for source_ref in selected_refs:
        manifest_document = manifest_by_ref.get(source_ref)
        document_id = (
            manifest_document.document_id
            if manifest_document is not None
            else rename_by_new_ref.get(source_ref)
        )
        if document_id is None:
            _fail("PROJECT.RECONCILE.INPUT_INVALID")
        assigned.append(document_id)

    assigned_ids = tuple(assigned)
    manifest_ids = frozenset(manifest_by_id)
    if (
        len(assigned_ids) != len(workspace.documents)
        or len(set(assigned_ids)) != len(assigned_ids)
        or frozenset(assigned_ids) != manifest_ids
    ):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")
    return tuple(zip(selected_refs, assigned_ids, strict=True))


def stage_workspace_rebind(
    root: Path,
    selected_paths: tuple[Path, ...],
    workspace: ProjectWorkspace,
    *,
    rename_mappings: tuple[OriginRenameMapping, ...] = (),
) -> StagedSelectedProjectDocuments:
    """Attach explicit source selections to an unbound package workspace.

    Identity authority comes only from the cold package manifest plus exact
    source-ref matches or explicit old-to-new mappings.  Source parsing and
    retained-root/no-follow proof remain owned by
    :func:`stage_selected_project_documents`.
    """

    assignments = _workspace_rebind_document_ids(
        root,
        selected_paths,
        workspace,
        rename_mappings,
    )
    expected_refs = tuple(source_ref for source_ref, _ in assignments)
    assigned_ids = tuple(document_id for _, document_id in assignments)
    staged = stage_selected_project_documents(
        root,
        selected_paths,
        SelectedProjectDocumentsRequest(
            name=workspace.name,
            source_locale=workspace.source_locale,
            target_locale=workspace.target_locale,
        ),
    )
    if (
        tuple(document.source_ref for document in staged.workspace.documents)
        != expected_refs
        or tuple(document.source_ref for document in staged.origin_binding.documents)
        != expected_refs
    ):
        _fail("PROJECT.RECONCILE.INPUT_INVALID")

    rebound_documents = tuple(
        replace(
            document,
            document_id=document_id,
            editing_overlay=tuple(
                replace(
                    overlay,
                    document_id=document_id,
                    saved_state_digest=editing_state_digest_v1(
                        document_id,
                        overlay.local_segment_id,
                        overlay.source_fingerprint,
                        overlay.target,
                        overlay.confirmed,
                    ),
                )
                for overlay in document.editing_overlay
            ),
        )
        for document, document_id in zip(
            staged.workspace.documents,
            assigned_ids,
            strict=True,
        )
    )
    rebound_workspace = replace(
        staged.workspace,
        project_id=workspace.project_id,
        documents=rebound_documents,
    )
    rebound_binding = replace(
        staged.origin_binding,
        project_id=workspace.project_id,
        documents=tuple(
            replace(document, document_id=document_id)
            for document, document_id in zip(
                staged.origin_binding.documents,
                assigned_ids,
                strict=True,
            )
        ),
    )
    return _issue_staged(
        rebound_workspace,
        rebound_binding,
        staged.source_identities,
    )


def revalidate_staged_selected_documents(
    staged: StagedSelectedProjectDocuments,
) -> StagedSelectedProjectDocuments:
    """Reparse the exact selected binding for preview/apply stale proof.

    The returned candidate is new Application evidence.  Callers compare its
    source identities with those retained by their private reconciliation
    operation; this function never mutates the staged workspace or source files.
    """

    if type(staged) is not StagedSelectedProjectDocuments:
        raise TypeError("staged must be exact StagedSelectedProjectDocuments")
    binding = staged.origin_binding
    root = Path(binding.absolute_root)
    selected_paths = tuple(root.joinpath(*item.source_ref.split("/")) for item in binding.documents)
    return stage_selected_project_documents(
        root,
        selected_paths,
        SelectedProjectDocumentsRequest(
            name=staged.workspace.name,
            source_locale=staged.workspace.source_locale,
            target_locale=staged.workspace.target_locale,
            origin_binding=binding,
            expected_binding_revision=binding.revision,
        ),
    )


__all__ = (
    "OriginBinding",
    "OriginRenameMapping",
    "SelectedProjectDocumentsRequest",
    "StagedSelectedProjectDocuments",
    "revalidate_staged_selected_documents",
    "stage_selected_project_documents",
    "stage_workspace_rebind",
)
