"""Cluster 1 contract tests for stable workspace identity and legacy JSON.

These tests intentionally exercise only the immutable C1 surface.  They do not
create a multi-document intake, ProjectPackage carrier, Controller session, or
Qt projection; those remain owned by later promotion clusters.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import unicodedata
from unittest.mock import patch

from editor_project import load_project, save_project
from editor_project_workspace_adapter import (
    load_legacy_single_json_workspace,
    workspace_to_legacy_editor_project,
)
from parser_contracts import CodecIdentity
from parser_composition import create_parser_application_surface
from project_workspace_contracts import (
    CodecPrivateMemberRef,
    EditingOverlayEntry,
    ProjectDocument,
    ProjectOrigin,
    ProjectOriginKind,
    ProjectPersistenceKind,
    ProjectSegment,
    ProjectSourceSegment,
    ProjectWorkspace,
    ProjectWorkspaceError,
    SegmentIdentity,
    WriterCapabilitySnapshot,
    require_workspace_segment_identity,
)
from project_workspace_identity import (
    EMPTY_SHA256,
    derive_explicit_selected_document_id,
    derive_legacy_single_json_document_id,
    derive_legacy_single_json_project_id,
    issue_project_id,
    normalize_portable_ref_v1,
    source_fingerprint_v1,
    validate_local_segment_id,
)


_PROJECT_A = "prj-" + "1" * 64
_PROJECT_B = "prj-" + "2" * 64
_DOCUMENT_A = "doc-" + "a" * 64
_DOCUMENT_B = "doc-" + "b" * 64
_DOCUMENT_C = "doc-" + "c" * 64
_DIGEST_A = hashlib.sha256(b"source-a").hexdigest()
_DIGEST_B = hashlib.sha256(b"source-b").hexdigest()
_EMPTY_STATE_DIGEST = hashlib.sha256(b"").hexdigest()


def _writer_snapshot() -> WriterCapabilitySnapshot:
    return WriterCapabilitySnapshot(
        canonical_write=True,
        source_round_trip_write=False,
        format_profile="localcat-json-v1",
    )


def _document(
    *,
    document_id: str = _DOCUMENT_A,
    source_ref: str = "chapters/one.json",
    display_name: str = "Chapter One",
    order: int = 0,
    local_segment_id: str = "shared",
    source: str = "First source",
    target: str = "第一段",
    source_fingerprint: str = _DIGEST_A,
) -> ProjectDocument:
    return ProjectDocument(
        document_id=document_id,
        source_ref=source_ref,
        display_name=display_name,
        order=order,
        format_id="localcat-json-v1",
        codec_identity=CodecIdentity(
            provider_id="localcat",
            codec_id="localcat-json",
            codec_version="1",
        ),
        writer_capability_snapshot=_writer_snapshot(),
        source_snapshot_digest=source_fingerprint,
        source_segments=(
            ProjectSourceSegment(
                local_segment_id=local_segment_id,
                source=source,
                raw_speaker="Narrator",
                source_fingerprint=source_fingerprint,
            ),
        ),
        editing_overlay=(
            EditingOverlayEntry(
                document_id=document_id,
                local_segment_id=local_segment_id,
                source_fingerprint=source_fingerprint,
                target=target,
                confirmed=True,
                saved_state_digest=_EMPTY_STATE_DIGEST,
            ),
        ),
        codec_private_member=None,
    )


def _workspace(
    *documents: ProjectDocument,
    project_id: str = _PROJECT_A,
    name: str = "Portable project",
    origin: ProjectOrigin | None = None,
    persistence_kind: ProjectPersistenceKind = ProjectPersistenceKind.PROJECT_PACKAGE,
) -> ProjectWorkspace:
    return ProjectWorkspace(
        schema_version=1,
        project_id=project_id,
        name=name,
        source_locale="en-US",
        target_locale="zh-CN",
        origin=origin
        or ProjectOrigin(
            kind=ProjectOriginKind.DIRECTORY,
            profile_version="explicit-selected-files-v1",
            portable_root_ref="project",
        ),
        persistence_kind=persistence_kind,
        documents=tuple(documents),
    )


def _document_identities(document: ProjectDocument) -> tuple[SegmentIdentity, ...]:
    return tuple(
        SegmentIdentity(document.document_id, segment.local_segment_id)
        for segment in document.source_segments
    )


def _legacy_identity_token(prefix: str, domain: bytes, value: str) -> str:
    encoded = value.encode("utf-8")
    digest = hashlib.sha256(
        domain + len(encoded).to_bytes(8, "big") + encoded
    ).hexdigest()
    return prefix + digest


class Cluster1IdentityContractTests(unittest.TestCase):
    def test_contract_graph_and_composite_identity_are_exactly_immutable(self) -> None:
        document = _document()
        workspace = _workspace(document)
        identity = SegmentIdentity(_DOCUMENT_A, "shared")
        segment_view = ProjectSegment(
            identity=identity,
            source="First source",
            target="第一段",
            raw_speaker="Narrator",
            confirmed=True,
            source_fingerprint=_DIGEST_A,
        )

        self.assertEqual(
            (identity.document_id, identity.local_segment_id),
            (_DOCUMENT_A, "shared"),
        )
        self.assertEqual(
            _document_identities(document),
            (identity,),
        )
        self.assertEqual(segment_view.identity, identity)
        self.assertEqual(workspace.documents, (document,))
        for exact_contract in (identity, segment_view, document, workspace):
            with self.subTest(exact_contract=type(exact_contract).__name__):
                self.assertFalse(hasattr(exact_contract, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            workspace.name = "forged"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            document.display_name = "forged"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            identity.local_segment_id = "forged"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            segment_view.target = "forged"  # type: ignore[misc]

    def test_same_local_segment_id_is_allowed_only_across_documents(self) -> None:
        first = _document(document_id=_DOCUMENT_A, order=0)
        second = _document(
            document_id=_DOCUMENT_B,
            source_ref="chapters/two.json",
            display_name="Chapter Two",
            order=1,
            source="Second source",
            target="第二段",
            source_fingerprint=_DIGEST_B,
        )

        workspace = _workspace(first, second)

        self.assertEqual(
            tuple(_document_identities(document) for document in workspace.documents),
            (
                (SegmentIdentity(_DOCUMENT_A, "shared"),),
                (SegmentIdentity(_DOCUMENT_B, "shared"),),
            ),
        )

    def test_distinct_documents_may_have_the_same_display_name(self) -> None:
        first = _document(document_id=_DOCUMENT_A, display_name="Chapter", order=0)
        second = _document(
            document_id=_DOCUMENT_B,
            source_ref="chapters/two.json",
            display_name="Chapter",
            order=1,
            source_fingerprint=_DIGEST_B,
        )

        workspace = _workspace(first, second)

        self.assertEqual(
            tuple(document.display_name for document in workspace.documents),
            ("Chapter", "Chapter"),
        )
        self.assertNotEqual(
            workspace.documents[0].document_id,
            workspace.documents[1].document_id,
        )

    def test_display_rename_and_reorder_never_reissue_identity(self) -> None:
        first = _document(document_id=_DOCUMENT_A, order=0)
        second = _document(
            document_id=_DOCUMENT_B,
            source_ref="chapters/two.json",
            display_name="Chapter Two",
            order=1,
            source_fingerprint=_DIGEST_B,
        )
        before = _workspace(first, second)

        renamed_second = replace(second, display_name="Appendix", order=0)
        renamed_first = replace(first, display_name="Opening", order=1)
        after = replace(before, documents=(renamed_second, renamed_first))

        self.assertEqual(before.project_id, after.project_id)
        self.assertEqual(
            {document.document_id for document in before.documents},
            {document.document_id for document in after.documents},
        )
        self.assertEqual(
            {
                identity
                for document in before.documents
                for identity in _document_identities(document)
            },
            {
                identity
                for document in after.documents
                for identity in _document_identities(document)
            },
        )

    def test_manifest_issued_ids_take_precedence_over_ref_and_display_changes(self) -> None:
        original = _document(document_id=_DOCUMENT_C)
        rebound = replace(
            original,
            source_ref="renamed/location.json",
            display_name="Completely different display",
        )

        workspace = _workspace(rebound, project_id=_PROJECT_B)

        self.assertEqual(workspace.project_id, _PROJECT_B)
        self.assertEqual(workspace.documents[0].document_id, _DOCUMENT_C)
        self.assertNotEqual(
            derive_explicit_selected_document_id("renamed/location.json"),
            _DOCUMENT_C,
        )

    def test_id_domains_are_separated_and_project_seed_is_injectable(self) -> None:
        source_ref = normalize_portable_ref_v1("chapters/cafe\u0301.json")
        device_local_origin_key = hashlib.sha256(
            b"verified-absolute-lexical-binding"
        ).hexdigest()
        project_id = derive_legacy_single_json_project_id(device_local_origin_key)
        document_id = derive_legacy_single_json_document_id(source_ref)
        intake_document_id = derive_explicit_selected_document_id(source_ref)

        self.assertRegex(project_id, r"^prj-[0-9a-f]{64}$")
        self.assertRegex(document_id, r"^doc-[0-9a-f]{64}$")
        self.assertRegex(intake_document_id, r"^doc-[0-9a-f]{64}$")
        self.assertEqual(len({project_id, document_id, intake_document_id}), 3)
        self.assertEqual(
            project_id,
            _legacy_identity_token(
                "prj-",
                b"localcat.project.single-json.v1\0",
                device_local_origin_key,
            ),
        )
        self.assertEqual(
            document_id,
            _legacy_identity_token(
                "doc-",
                b"localcat.document.single-json.v1\0",
                source_ref,
            ),
        )
        self.assertEqual(
            intake_document_id,
            "doc-"
            + hashlib.sha256(
                b"localcat.document.explicit-selected-files.v1\0"
                + source_ref.encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(issue_project_id(b"\x01" * 32), issue_project_id(b"\x01" * 32))
        self.assertNotEqual(issue_project_id(b"\x01" * 32), issue_project_id(b"\x02" * 32))

    def test_project_context_is_required_to_resolve_a_composite_identity(self) -> None:
        identity = SegmentIdentity(_DOCUMENT_A, "shared")
        workspace_a = _workspace(_document(), project_id=_PROJECT_A)
        workspace_b = _workspace(_document(), project_id=_PROJECT_B)

        resolved_a = require_workspace_segment_identity(
            workspace_a,
            _PROJECT_A,
            identity,
        )
        resolved_b = require_workspace_segment_identity(
            workspace_b,
            _PROJECT_B,
            identity,
        )

        self.assertIsInstance(resolved_a, ProjectSegment)
        self.assertEqual(resolved_a.identity, identity)
        self.assertEqual(resolved_b.identity, identity)
        hostile = (
            (_PROJECT_B, identity),
            (_PROJECT_A, SegmentIdentity(_DOCUMENT_B, "shared")),
            (_PROJECT_A, SegmentIdentity(_DOCUMENT_A, "not-issued")),
        )
        for expected_project_id, candidate in hostile:
            with self.subTest(
                expected_project_id=expected_project_id,
                candidate=candidate,
            ), self.assertRaises(ProjectWorkspaceError):
                require_workspace_segment_identity(
                    workspace_a,
                    expected_project_id,
                    candidate,
                )

    def test_forged_duplicate_and_cross_document_identity_fail_closed(self) -> None:
        invalid_factories = {
            "bad-project-shape": lambda: _workspace(
                _document(), project_id="prj-not-a-token"
            ),
            "bad-document-shape": lambda: _workspace(
                _document(document_id="doc-not-a-token")
            ),
            "duplicate-document": lambda: _workspace(
                _document(document_id=_DOCUMENT_A, order=0),
                _document(
                    document_id=_DOCUMENT_A,
                    source_ref="chapters/two.json",
                    order=1,
                ),
            ),
            "cross-document-overlay": lambda: replace(
                _document(document_id=_DOCUMENT_A),
                editing_overlay=(
                    EditingOverlayEntry(
                        document_id=_DOCUMENT_B,
                        local_segment_id="shared",
                        source_fingerprint=_DIGEST_A,
                        target="forged",
                        confirmed=False,
                        saved_state_digest=_EMPTY_STATE_DIGEST,
                    ),
                ),
            ),
            "missing-local-id": lambda: replace(
                _document(),
                editing_overlay=(
                    EditingOverlayEntry(
                        document_id=_DOCUMENT_A,
                        local_segment_id="not-issued",
                        source_fingerprint=_DIGEST_A,
                        target="forged",
                        confirmed=False,
                        saved_state_digest=_EMPTY_STATE_DIGEST,
                    ),
                ),
            ),
            "overlay-fingerprint-mismatch": lambda: replace(
                _document(),
                editing_overlay=(
                    EditingOverlayEntry(
                        document_id=_DOCUMENT_A,
                        local_segment_id="shared",
                        source_fingerprint=_DIGEST_B,
                        target="forged",
                        confirmed=False,
                        saved_state_digest=_EMPTY_STATE_DIGEST,
                    ),
                ),
            ),
            "duplicate-local-id": lambda: replace(
                _document(),
                source_segments=(
                    ProjectSourceSegment("same", "One", "", _DIGEST_A),
                    ProjectSourceSegment("same", "Two", "", _DIGEST_B),
                ),
                editing_overlay=(
                    EditingOverlayEntry(
                        _DOCUMENT_A,
                        "same",
                        _DIGEST_A,
                        "",
                        False,
                        _EMPTY_STATE_DIGEST,
                    ),
                ),
            ),
            "duplicate-overlay-id": lambda: replace(
                _document(),
                editing_overlay=(
                    EditingOverlayEntry(
                        _DOCUMENT_A,
                        "shared",
                        _DIGEST_A,
                        "first",
                        False,
                        _EMPTY_STATE_DIGEST,
                    ),
                    EditingOverlayEntry(
                        _DOCUMENT_A,
                        "shared",
                        _DIGEST_A,
                        "second",
                        False,
                        _EMPTY_STATE_DIGEST,
                    ),
                ),
            ),
        }
        for case, factory in invalid_factories.items():
            with self.subTest(case=case), self.assertRaises(ProjectWorkspaceError):
                factory()

    def test_local_segment_id_rejects_control_surrogate_and_limit_violations(self) -> None:
        invalid_local_ids = (
            "",
            "bad\x00id",
            "bad\x1fid",
            "bad\x85id",
            "bad\ud800id",
            "x" * 1025,
        )
        for local_segment_id in invalid_local_ids:
            with self.subTest(local_segment_id=ascii(local_segment_id)):
                with self.assertRaises(ProjectWorkspaceError):
                    ProjectSourceSegment(
                        local_segment_id=local_segment_id,
                        source="Source",
                        raw_speaker="",
                        source_fingerprint=_DIGEST_A,
                    )
        for blank_id in (" ", "\N{NO-BREAK SPACE}"):
            with self.subTest(blank_id=ascii(blank_id)):
                with self.assertRaises(ProjectWorkspaceError) as error:
                    ProjectSourceSegment(
                        local_segment_id=blank_id,
                        source="Source",
                        raw_speaker="",
                        source_fingerprint=_DIGEST_A,
                    )
                self.assertEqual(
                    error.exception.code,
                    "PROJECT.WORKSPACE.CONTRACT_INVALID",
                )

    def test_dense_document_order_and_exact_container_types_are_enforced(self) -> None:
        with self.assertRaises(ProjectWorkspaceError):
            _workspace(
                _document(document_id=_DOCUMENT_A, order=1),
                _document(
                    document_id=_DOCUMENT_B,
                    source_ref="chapters/two.json",
                    order=0,
                ),
            )
        with self.assertRaises((TypeError, ProjectWorkspaceError)):
            replace(_workspace(_document()), documents=[_document()])  # type: ignore[arg-type]

        invalid_exact_factories = (
            lambda: replace(_workspace(_document()), schema_version=True),
            lambda: replace(_document(), order=True),
            lambda: WriterCapabilitySnapshot(
                canonical_write=1,  # type: ignore[arg-type]
                source_round_trip_write=False,
                format_profile="localcat-json-v1",
            ),
            lambda: replace(
                _document(),
                source_segments=list(_document().source_segments),  # type: ignore[arg-type]
            ),
        )
        for factory in invalid_exact_factories:
            with self.subTest(factory=factory), self.assertRaises(ProjectWorkspaceError):
                factory()


class Cluster1OriginAndPortableRefTests(unittest.TestCase):
    def test_origin_kind_is_a_closed_exact_three_leaf_contract(self) -> None:
        self.assertEqual(
            tuple((kind.name, kind.value) for kind in ProjectOriginKind),
            (
                ("SINGLE_FILE", "single_file"),
                ("DIRECTORY", "directory"),
                ("WORKBOOK", "workbook"),
            ),
        )
        origins = tuple(
            ProjectOrigin(
                kind=kind,
                profile_version="profile-v1",
                portable_root_ref="source",
            )
            for kind in ProjectOriginKind
        )
        self.assertEqual(tuple(origin.kind for origin in origins), tuple(ProjectOriginKind))
        with self.assertRaises((TypeError, ProjectWorkspaceError)):
            ProjectOrigin(  # type: ignore[arg-type]
                kind="directory",
                profile_version="profile-v1",
                portable_root_ref="source",
            )

    def test_portable_ref_normalizes_nfc_but_keeps_case_sensitive_identity(self) -> None:
        decomposed = "chapters/" + unicodedata.normalize("NFD", "café") + ".json"

        normalized = normalize_portable_ref_v1(decomposed)

        self.assertEqual(normalized, "chapters/café.json")
        self.assertEqual(normalize_portable_ref_v1("Chapter.json"), "Chapter.json")
        self.assertNotEqual(
            normalize_portable_ref_v1("Chapter.json"),
            normalize_portable_ref_v1("chapter.json"),
        )

    def test_portable_ref_rejects_unsafe_or_ambiguous_spellings(self) -> None:
        invalid_refs = (
            "",
            "/absolute.json",
            "trailing/",
            "a//b.json",
            "./chapter.json",
            "../chapter.json",
            "a/../chapter.json",
            r"chapters\one.json",
            "C:/chapter.json",
            "file://chapter.json",
            "chapter:one.json",
            "chapter.json.",
            "chapter.json ",
            "chapter\x00.json",
            "chapter\x1f.json",
            "chapter\x85.json",
            "bad\ud800.json",
            "CON",
            "NUL.txt",
            "aux",
            "COM1.json",
            "COM¹.json",
            "LPT².txt",
            'bad"name.json',
            "bad<name.json",
            "bad>name.json",
            "bad|name.json",
            "bad?name.json",
            "bad*name.json",
            "x" * 256 + ".json",
            "x/" + "y" * 1022,
        )
        for source_ref in invalid_refs:
            with self.subTest(source_ref=ascii(source_ref)):
                with self.assertRaises(ProjectWorkspaceError) as error:
                    normalize_portable_ref_v1(source_ref)
                self.assertEqual(str(error.exception), error.exception.code)

    def test_portable_ref_segment_limit_is_exactly_255_utf8_bytes(self) -> None:
        accepted = "x" * 250 + ".json"
        rejected = "x" * 251 + ".json"

        self.assertEqual(len(accepted.encode("utf-8")), 255)
        self.assertEqual(normalize_portable_ref_v1(accepted), accepted)
        self.assertEqual(len(rejected.encode("utf-8")), 256)
        with self.assertRaises(ProjectWorkspaceError) as error:
            normalize_portable_ref_v1(rejected)
        self.assertEqual(
            error.exception.code,
            "PROJECT.WORKSPACE.LIMIT_EXCEEDED",
        )

    def test_slash_is_path_syntax_not_a_control_character_in_opaque_ids(self) -> None:
        self.assertEqual(
            validate_local_segment_id("chapter/line"),
            "chapter/line",
        )
        self.assertEqual(
            normalize_portable_ref_v1("chapter/line.json"),
            "chapter/line.json",
        )

    def test_workspace_error_code_is_closed_body_safe_and_immutable(self) -> None:
        hostile_codes = (
            "PROJECT.LEAK",
            "PROJECT./Users/alice/secret.json",
            "PROJECT.WORKSPACE.CONTRACT_INVALID\nsource-body",
        )
        for code in hostile_codes:
            with self.subTest(code=code), self.assertRaises(ValueError):
                ProjectWorkspaceError(code)

        error = ProjectWorkspaceError("PROJECT.WORKSPACE.CONTRACT_INVALID")
        with self.assertRaises(AttributeError):
            error.code = "PROJECT.WORKSPACE.PATH_INVALID"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            error.retryable = True  # type: ignore[misc]
        error.__dict__["code"] = "/private/source/body"
        self.assertEqual(str(error), "PROJECT.WORKSPACE.CONTRACT_INVALID")
        self.assertEqual(error.code, "PROJECT.WORKSPACE.CONTRACT_INVALID")

    def test_workspace_rejects_casefold_and_nfc_source_ref_collisions(self) -> None:
        collisions = (
            ("chapters/same.json", "chapters/same.json"),
            ("chapters/Chapter.json", "chapters/chapter.json"),
            ("chapters/café.json", "chapters/cafe\u0301.json"),
        )
        for first_ref, second_ref in collisions:
            with self.subTest(first_ref=first_ref, second_ref=second_ref):
                first = _document(
                    document_id=_DOCUMENT_A,
                    source_ref=normalize_portable_ref_v1(first_ref),
                    order=0,
                )
                second = _document(
                    document_id=_DOCUMENT_B,
                    source_ref=normalize_portable_ref_v1(second_ref),
                    order=1,
                    source_fingerprint=_DIGEST_B,
                )
                with self.assertRaises(ProjectWorkspaceError):
                    _workspace(first, second)

    def test_workbook_leaf_allows_only_exact_shared_source_ref(self) -> None:
        first = _document(
            document_id=_DOCUMENT_A,
            source_ref="book.xlsx",
            order=0,
        )
        second = _document(
            document_id=_DOCUMENT_B,
            source_ref="book.xlsx",
            order=1,
            source_fingerprint=_DIGEST_B,
        )
        origin = ProjectOrigin(
            kind=ProjectOriginKind.WORKBOOK,
            profile_version="workbook-descriptor-v1",
            portable_root_ref="book.xlsx",
        )

        workspace = _workspace(first, second, origin=origin)

        self.assertEqual(
            tuple(document.source_ref for document in workspace.documents),
            ("book.xlsx", "book.xlsx"),
        )
        with self.assertRaises(ProjectWorkspaceError):
            _workspace(
                first,
                replace(second, source_ref="Book.xlsx"),
                origin=origin,
            )

    def test_source_fingerprint_has_an_exact_source_only_hash_vector(self) -> None:
        source = "Source 📖"
        speaker = "Narrator"
        expected = hashlib.sha256(
            b"localcat.segment-source.v1\0"
            + len(source.encode("utf-8")).to_bytes(8, "big")
            + source.encode("utf-8")
            + len(speaker.encode("utf-8")).to_bytes(8, "big")
            + speaker.encode("utf-8")
            + bytes.fromhex(EMPTY_SHA256)
        ).hexdigest()

        self.assertEqual(source_fingerprint_v1(source, speaker), expected)


class Cluster1LegacyJsonAdapterTests(unittest.TestCase):
    def test_real_single_json_public_journey_is_exactly_compatible(self) -> None:
        payload = {
            "schema_version": 1,
            "name": "Legacy chapter",
            "source_locale": "en-GB",
            "target_locale": "zh-Hans",
            "segments": [
                {
                    "id": "line-a",
                    "source": "Hello",
                    "target": "你好",
                    "speaker": "Narrator",
                    "confirmed": True,
                },
                {"id": "line-b", "source": "World"},
            ],
        }
        with tempfile.TemporaryDirectory(prefix="localcat-c1-legacy-") as temporary:
            path = Path(temporary) / "chapter.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            legacy = load_project(path)
            workspace = load_legacy_single_json_workspace(path)
            projected = workspace_to_legacy_editor_project(workspace, path=path)
            saved_path = Path(temporary) / "cold-reopen.json"
            save_project(projected, saved_path)
            cold_reopened = load_project(saved_path)

        self.assertEqual(projected, legacy)
        self.assertEqual(cold_reopened, replace(projected, path=saved_path))
        self.assertEqual(workspace.origin.kind, ProjectOriginKind.SINGLE_FILE)
        self.assertEqual(
            workspace.persistence_kind,
            ProjectPersistenceKind.LEGACY_SINGLE_JSON,
        )
        self.assertEqual(len(workspace.documents), 1)
        self.assertEqual(
            tuple(
                (
                    segment.local_segment_id,
                    segment.source,
                    overlay.target,
                    segment.raw_speaker,
                    overlay.confirmed,
                )
                for segment, overlay in zip(
                    workspace.documents[0].source_segments,
                    workspace.documents[0].editing_overlay,
                    strict=True,
                )
            ),
            (
                ("line-a", "Hello", "你好", "Narrator", True),
                ("line-b", "World", "", "", False),
            ),
        )

    def test_same_binding_reopen_keeps_ids_and_source_fingerprint_has_exact_boundary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="localcat-c1-reopen-") as temporary:
            path = Path(temporary) / "stable.json"

            def write(*, source: str, target: str, confirmed: bool) -> None:
                path.write_text(
                    json.dumps(
                        {
                            "segments": [
                                {
                                    "id": "line",
                                    "source": source,
                                    "target": target,
                                    "speaker": "Narrator",
                                    "confirmed": confirmed,
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            write(source="Stable source", target="old target", confirmed=False)
            first = load_legacy_single_json_workspace(path)
            second = load_legacy_single_json_workspace(path)
            write(source="Stable source", target="new target", confirmed=True)
            target_only_changed = load_legacy_single_json_workspace(path)
            write(source="Changed source", target="new target", confirmed=True)
            source_changed = load_legacy_single_json_workspace(path)

        def identities(workspace: ProjectWorkspace) -> tuple[object, ...]:
            document = workspace.documents[0]
            return (
                workspace.project_id,
                document.document_id,
                _document_identities(document),
            )

        self.assertEqual(identities(first), identities(second))
        self.assertEqual(identities(first), identities(target_only_changed))
        self.assertEqual(identities(first), identities(source_changed))
        first_fingerprint = first.documents[0].source_segments[0].source_fingerprint
        self.assertEqual(
            first_fingerprint,
            target_only_changed.documents[0].source_segments[0].source_fingerprint,
        )
        self.assertNotEqual(
            first_fingerprint,
            source_changed.documents[0].source_segments[0].source_fingerprint,
        )
        self.assertEqual(
            target_only_changed.documents[0].editing_overlay[0].target,
            "new target",
        )
        self.assertTrue(
            target_only_changed.documents[0].editing_overlay[0].confirmed
        )

    def test_legacy_display_and_locale_text_remain_exact_without_unapproved_cap(
        self,
    ) -> None:
        name = "Chapter\nName"
        source_locale = "x-" + "a" * 256
        target_locale = "y-" + "b" * 256
        with tempfile.TemporaryDirectory(prefix="localcat-c1-text-") as temporary:
            path = Path(temporary) / "chapter.json"
            path.write_text(
                json.dumps(
                    {
                        "name": name,
                        "source_locale": source_locale,
                        "target_locale": target_locale,
                        "segments": [{"id": "safe-id", "source": "Source"}],
                    }
                ),
                encoding="utf-8",
            )

            legacy = load_project(path)
            workspace = load_legacy_single_json_workspace(path)
            projected = workspace_to_legacy_editor_project(workspace, path=path)

        self.assertEqual(projected, legacy)
        self.assertEqual(workspace.name, name)
        self.assertEqual(workspace.source_locale, source_locale)
        self.assertEqual(workspace.target_locale, target_locale)

    def test_workspace_v1_rejects_unsafe_legacy_promotion_without_touching_file(
        self,
    ) -> None:
        cases = (
            (
                "control-id",
                "Legacy",
                "line\x00id",
                "PROJECT.WORKSPACE.CONTRACT_INVALID",
            ),
            (
                "long-id",
                "Legacy",
                "x" * 1_025,
                "PROJECT.WORKSPACE.LIMIT_EXCEEDED",
            ),
            (
                "long-name",
                "N" * 513,
                "line",
                "PROJECT.WORKSPACE.LIMIT_EXCEEDED",
            ),
        )
        with tempfile.TemporaryDirectory(prefix="localcat-c1-eligibility-") as temporary:
            for case, name, local_id, expected_code in cases:
                with self.subTest(case=case):
                    path = Path(temporary) / f"{case}.json"
                    path.write_text(
                        json.dumps(
                            {
                                "name": name,
                                "segments": [
                                    {
                                        "id": local_id,
                                        "source": "Source",
                                        "target": "Target",
                                        "confirmed": True,
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                    legacy = load_project(path)
                    save_project(legacy, path)
                    cold_reopened = load_project(path)
                    before = path.read_bytes()

                    with self.assertRaises(ProjectWorkspaceError) as error:
                        load_legacy_single_json_workspace(path)

                    self.assertEqual(error.exception.code, expected_code)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(load_project(path), cold_reopened)

            for unsafe_basename in (
                "legacy?.json",
                "legacy:name.json",
                "NUL.json",
            ):
                with self.subTest(unsafe_basename=unsafe_basename):
                    unsafe_path = Path(temporary) / unsafe_basename
                    unsafe_path.write_text(
                        json.dumps(
                            {"segments": [{"id": "line", "source": "Source"}]}
                        ),
                        encoding="utf-8",
                    )
                    unsafe_legacy = load_project(unsafe_path)
                    save_project(unsafe_legacy, unsafe_path)
                    unsafe_before = unsafe_path.read_bytes()

                    with self.assertRaises(ProjectWorkspaceError) as path_error:
                        load_legacy_single_json_workspace(unsafe_path)

                    self.assertEqual(
                        path_error.exception.code,
                        "PROJECT.WORKSPACE.PATH_INVALID",
                    )
                    self.assertEqual(unsafe_path.read_bytes(), unsafe_before)
                    self.assertEqual(load_project(unsafe_path), unsafe_legacy)

    def test_unverified_or_fatal_json_never_publishes_a_workspace(self) -> None:
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "parser"
            / "project"
            / "payloads"
            / "localcat-fatal-tail.json"
        )
        with tempfile.TemporaryDirectory(prefix="localcat-c1-fatal-") as temporary:
            path = Path(temporary) / "fatal.json"
            path.write_bytes(fixture.read_bytes())

            with self.assertRaises(ProjectWorkspaceError) as error:
                load_legacy_single_json_workspace(path)

        self.assertEqual(str(error.exception), error.exception.code)
        self.assertNotIn("fatal.json", str(error.exception))

    def test_descriptor_terminal_identity_mismatch_never_publishes(self) -> None:
        real_surface = create_parser_application_surface()

        class MismatchedOpened:
            def __init__(self, opened, mismatch: str) -> None:
                self._opened = opened
                self.descriptor = (
                    replace(
                        opened.descriptor,
                        identity=CodecIdentity("forged", "localcat-json", "1"),
                    )
                    if mismatch == "descriptor"
                    else opened.descriptor
                )
                self.source_identity = (
                    replace(
                        opened.source_identity,
                        content_sha256="f" * 64,
                    )
                    if mismatch == "source"
                    else opened.source_identity
                )

            def __enter__(self):
                self._opened.__enter__()
                return self

            def __exit__(self, exc_type, exc, traceback):
                return self._opened.__exit__(exc_type, exc, traceback)

            def stream(self):
                return self._opened.stream()

        class MismatchedSurface:
            def __init__(self, mismatch: str) -> None:
                self._mismatch = mismatch

            def open_input(self, *args, **kwargs):
                return MismatchedOpened(
                    real_surface.open_input(*args, **kwargs),
                    self._mismatch,
                )

        with tempfile.TemporaryDirectory(prefix="localcat-c1-mismatch-") as temporary:
            path = Path(temporary) / "chapter.json"
            path.write_text(
                json.dumps([{"id": "one", "source": "Source"}]),
                encoding="utf-8",
            )
            for mismatch in ("descriptor", "source"):
                with self.subTest(mismatch=mismatch), patch(
                    "editor_project_workspace_adapter.create_parser_application_surface",
                    return_value=MismatchedSurface(mismatch),
                ), self.assertRaises(ProjectWorkspaceError) as error:
                    load_legacy_single_json_workspace(path)

        self.assertEqual(str(error.exception), error.exception.code)

    def test_descriptor_format_and_terminal_profile_mismatch_never_publish(self) -> None:
        real_surface = create_parser_application_surface()

        class MismatchedOpened:
            def __init__(self, opened, *, terminal_profile: bool) -> None:
                self._opened = opened
                self.source_identity = opened.source_identity
                self.descriptor = (
                    opened.descriptor
                    if terminal_profile
                    else replace(
                        opened.descriptor,
                        format_id=type(opened.descriptor.format_id)("line-text-v1"),
                    )
                )
                self._terminal_profile = terminal_profile

            def __enter__(self):
                self._opened.__enter__()
                return self

            def __exit__(self, exc_type, exc, traceback):
                return self._opened.__exit__(exc_type, exc, traceback)

            def stream(self):
                session = self._opened.stream()
                if not self._terminal_profile:
                    return session

                original_verified_terminal = session.verified_terminal

                def mismatched_terminal():
                    terminal = original_verified_terminal()
                    object.__setattr__(terminal, "limit_profile", replace(
                        terminal.limit_profile,
                        profile_version=terminal.limit_profile.profile_version + 1,
                    ))
                    return terminal

                session.verified_terminal = mismatched_terminal
                return session

        class MismatchedSurface:
            def __init__(self, *, terminal_profile: bool) -> None:
                self._terminal_profile = terminal_profile

            def open_input(self, *args, **kwargs):
                return MismatchedOpened(
                    real_surface.open_input(*args, **kwargs),
                    terminal_profile=self._terminal_profile,
                )

        with tempfile.TemporaryDirectory(prefix="localcat-c1-descriptor-") as temporary:
            path = Path(temporary) / "chapter.json"
            path.write_text(
                json.dumps([{"id": "one", "source": "Source"}]),
                encoding="utf-8",
            )
            for terminal_profile in (False, True):
                with self.subTest(terminal_profile=terminal_profile), patch(
                    "editor_project_workspace_adapter.create_parser_application_surface",
                    return_value=MismatchedSurface(
                        terminal_profile=terminal_profile,
                    ),
                ), self.assertRaises(ProjectWorkspaceError):
                    load_legacy_single_json_workspace(path)

    def test_preopen_oserror_is_body_safe_but_programmer_fault_is_visible(self) -> None:
        with self.assertRaises(ProjectWorkspaceError) as home_error:
            load_legacy_single_json_workspace(
                Path("~localcat-no-such-user-987654/chapter.json")
            )
        self.assertEqual(
            str(home_error.exception),
            "PROJECT.WORKSPACE.CONTRACT_INVALID",
        )

        with patch(
            "editor_project_workspace_adapter.os.path.abspath",
            side_effect=FileNotFoundError("/private/source/body"),
        ):
            with self.assertRaises(ProjectWorkspaceError) as error:
                load_legacy_single_json_workspace(Path("chapter.json"))
        self.assertEqual(str(error.exception), "PROJECT.WORKSPACE.CONTRACT_INVALID")
        self.assertNotIn("private", str(error.exception))

        with patch(
            "editor_project_workspace_adapter.os.path.abspath",
            side_effect=AssertionError("programmer-fault"),
        ):
            with self.assertRaisesRegex(AssertionError, "programmer-fault"):
                load_legacy_single_json_workspace(Path("chapter.json"))

    def test_array_root_public_journey_preserves_default_ids_and_order(self) -> None:
        payload = (
            {
                "id": "z-last",
                "source": "First",
                "target": "一",
                "speaker": "A",
                "confirmed": True,
            },
            {"source": "Second", "speaker": "B"},
        )
        with tempfile.TemporaryDirectory(prefix="localcat-c1-array-") as temporary:
            path = Path(temporary) / "array.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            legacy = load_project(path)
            workspace = load_legacy_single_json_workspace(path)
            projected = workspace_to_legacy_editor_project(workspace, path=path)

        self.assertEqual(projected, legacy)
        self.assertEqual(
            tuple(
                segment.local_segment_id
                for segment in workspace.documents[0].source_segments
            ),
            ("z-last", "segment-2"),
        )

    def test_legacy_projection_rejects_multiple_documents_without_flattening(self) -> None:
        first = _document(document_id=_DOCUMENT_A, order=0)
        second = _document(
            document_id=_DOCUMENT_B,
            source_ref="chapters/two.json",
            order=1,
            source_fingerprint=_DIGEST_B,
        )
        workspace = _workspace(first, second)

        with self.assertRaises(ProjectWorkspaceError):
            workspace_to_legacy_editor_project(workspace)

    def test_legacy_projection_rejects_wrong_origin_persistence_or_format(self) -> None:
        valid = _workspace(
            _document(source_ref="chapter.json"),
            name="Chapter One",
            origin=ProjectOrigin(
                ProjectOriginKind.SINGLE_FILE,
                "localcat-json-v1",
                "chapter.json",
            ),
            persistence_kind=ProjectPersistenceKind.LEGACY_SINGLE_JSON,
        )
        invalid_workspace_factories = {
            "directory-origin": lambda: replace(
                valid,
                origin=ProjectOrigin(
                    ProjectOriginKind.DIRECTORY,
                    "explicit-selected-files-v1",
                    "project",
                ),
            ),
            "project-package-persistence": lambda: replace(
                valid,
                persistence_kind=ProjectPersistenceKind.PROJECT_PACKAGE,
            ),
            "wrong-format": lambda: replace(
                valid,
                documents=(replace(valid.documents[0], format_id="line-text-v1"),),
            ),
            "wrong-codec": lambda: replace(
                valid,
                documents=(
                    replace(
                        valid.documents[0],
                        codec_identity=CodecIdentity(
                            "third-party", "lookalike-json", "1"
                        ),
                    ),
                ),
            ),
            "display-name-loss": lambda: replace(
                valid,
                documents=(replace(valid.documents[0], display_name="Other"),),
            ),
            "codec-private-loss": lambda: replace(
                valid,
                documents=(
                    replace(
                        valid.documents[0],
                        codec_private_member=CodecPrivateMemberRef(
                            member_path="codec-private/blob.bin",
                            sha256=_DIGEST_A,
                            byte_count=1,
                            codec_identity=valid.documents[0].codec_identity,
                            profile_version="localcat-json-v1",
                        ),
                    ),
                ),
            ),
        }
        for case, factory in invalid_workspace_factories.items():
            with self.subTest(case=case), self.assertRaises(ProjectWorkspaceError):
                workspace_to_legacy_editor_project(factory())


if __name__ == "__main__":
    unittest.main()
