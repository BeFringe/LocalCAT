from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from editor_contracts import EditorProject, EditorSegment
from editor_controller import EditorController, EditorControllerError
from editor_project import ProjectError, load_project, save_project
from parser_contracts import (
    CanonicalSerializeRequest,
    ContractViolation,
    DocumentHeader,
    EffectivePurpose,
    LINE_TEXT_V1,
    LOCALCAT_JSON_V1,
    ParsedSegment,
    RawSpeaker,
    SourceReference,
    TargetPresence,
    TargetReference,
    TranslationState,
)
from resource_repository import ResourceRepository


class ProjectFacadeCharacterizationTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: object) -> None:
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8-sig",
        )

    def test_array_root_uses_compatibility_defaults_and_preserves_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "array-project.json"
            self._write_json(
                path,
                [
                    {
                        "id": "  explicit-id  ",
                        "source": "  First  source  ",
                        "target": "  First  target  ",
                        "speaker": "  Speaker  One  ",
                        "confirmed": True,
                    },
                    {
                        "id": "   ",
                        "source": "Second  source",
                        "target": None,
                        "speaker": None,
                    },
                    {"source": "Third source"},
                ],
            )

            project = load_project(path)

        self.assertEqual(project.name, "array-project")
        self.assertEqual(project.source_locale, "en-US")
        self.assertEqual(project.target_locale, "zh-CN")
        self.assertEqual(project.path, path.absolute())
        self.assertEqual(
            tuple(segment.id for segment in project.segments),
            ("explicit-id", "segment-2", "segment-3"),
        )
        self.assertEqual(
            tuple(segment.source for segment in project.segments),
            ("First  source", "Second  source", "Third source"),
        )
        self.assertEqual(project.segments[0].target, "First  target")
        self.assertEqual(project.segments[0].speaker, "Speaker  One")
        self.assertTrue(project.segments[0].confirmed)
        self.assertEqual(project.segments[1].target, "")
        self.assertEqual(project.segments[1].speaker, "")
        self.assertFalse(project.segments[1].confirmed)

    def test_object_root_defaults_blank_metadata_and_keeps_ids_document_local(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_path = root / "first.json"
            second_path = root / "second.json"
            self._write_json(
                first_path,
                {
                    "schema_version": 1,
                    "name": "   ",
                    "source_locale": "   ",
                    "segments": [{"id": "shared", "source": " First "}],
                },
            )
            self._write_json(
                second_path,
                {
                    "name": " Second  chapter ",
                    "source_locale": " ja-JP ",
                    "target_locale": " zh-Hans ",
                    "segments": [{"id": "shared", "source": " Second "}],
                },
            )

            first = load_project(first_path)
            second = load_project(second_path)

        self.assertEqual(first.name, "first")
        self.assertEqual(first.source_locale, "en-US")
        self.assertEqual(first.target_locale, "zh-CN")
        self.assertEqual(first.segments[0].id, "shared")
        self.assertEqual(first.segments[0].source, "First")
        self.assertEqual(second.name, "Second  chapter")
        self.assertEqual(second.source_locale, "ja-JP")
        self.assertEqual(second.target_locale, "zh-Hans")
        self.assertEqual(second.segments[0].id, "shared")
        self.assertEqual(second.segments[0].source, "Second")

    def test_invalid_json_inputs_fail_as_whole_projects(self) -> None:
        invalid_payloads = {
            "scalar-root": "not-a-project",
            "missing-segments": {"name": "Missing"},
            "empty-project": {"segments": []},
            "non-object-segment": ["not-an-object"],
            "missing-source": [{"id": "one"}],
            "empty-source": [{"source": "   "}],
            "numeric-source": [{"source": 1}],
            "numeric-target": [{"source": "One", "target": 1}],
            "numeric-speaker": [{"source": "One", "speaker": 1}],
            "non-boolean-confirmed": [{"source": "One", "confirmed": 1}],
            "duplicate-id": [
                {"id": "same", "source": "One"},
                {"id": " same ", "source": "Two"},
            ],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for case, payload in invalid_payloads.items():
                with self.subTest(case=case):
                    path = root / f"{case}.json"
                    self._write_json(path, payload)
                    with self.assertRaises(ProjectError):
                        load_project(path)

    def test_txt_is_source_only_with_dense_ids_and_empty_project_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "chapter.TXT"
            path.write_text(
                "\ufeff First line \n\n   \nSecond  line\r\n Third line ",
                encoding="utf-8",
            )
            empty_path = root / "empty.txt"
            empty_path.write_text("\ufeff\n \n\t\n", encoding="utf-8")

            project = load_project(path)
            with self.assertRaises(ProjectError):
                load_project(empty_path)

        self.assertEqual(project.name, "chapter")
        self.assertEqual(
            tuple(segment.id for segment in project.segments),
            ("segment-1", "segment-2", "segment-3"),
        )
        self.assertEqual(
            tuple(segment.source for segment in project.segments),
            ("First line", "Second  line", "Third line"),
        )
        self.assertTrue(all(segment.target == "" for segment in project.segments))
        self.assertTrue(all(segment.speaker == "" for segment in project.segments))
        self.assertTrue(all(not segment.confirmed for segment in project.segments))

    def test_load_waits_for_verified_terminal_before_returning_staged_segments(self) -> None:
        class FailingSession:
            closed = False

            def __iter__(self):
                return iter(
                    (
                        DocumentHeader("Staged", "en-US", "zh-CN", ()),
                        ParsedSegment(
                            local_id="one",
                            source="Provisional source",
                            target="暂存",
                            target_presence=TargetPresence.PRESENT,
                            translation_state=TranslationState.CONFIRMED,
                            speaker=RawSpeaker("Narrator"),
                            format_metadata=(),
                        ),
                    )
                )

            def verified_terminal(self):
                raise ContractViolation(
                    "PARSER.SOURCE.STALE",
                    "sealed source changed before terminal proof",
                )

            def close(self) -> None:
                self.closed = True

        class OpenedInput:
            def __init__(self, session: FailingSession) -> None:
                self.session = session

            def stream(self) -> FailingSession:
                return self.session

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                return None

        class Surface:
            def __init__(self, opened: OpenedInput) -> None:
                self.opened = opened

            def open_input(self, _reference, _selection, _request):
                return self.opened

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "staged.json"
            self._write_json(path, [{"source": "Old parser would accept this"}])
            session = FailingSession()
            surface = Surface(OpenedInput(session))

            with patch(
                "editor_project.create_parser_application_surface",
                return_value=surface,
            ):
                with self.assertRaises(ProjectError):
                    load_project(path)

        self.assertTrue(session.closed)

    def test_load_selects_project_format_and_passes_unresolved_rooted_reference(self) -> None:
        captured: dict[str, object] = {}

        class SuccessfulSession:
            def __iter__(self):
                return iter(
                    (
                        DocumentHeader("chapter", None, None, ()),
                        ParsedSegment(
                            local_id="segment-1",
                            source="Source",
                            target=None,
                            target_presence=TargetPresence.MISSING,
                            translation_state=None,
                            speaker=RawSpeaker(""),
                            format_metadata=(),
                        ),
                    )
                )

            def verified_terminal(self):
                return object()

            def close(self) -> None:
                return None

        class OpenedInput:
            def stream(self) -> SuccessfulSession:
                return SuccessfulSession()

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback) -> None:
                return None

        class Surface:
            def open_input(self, reference, selection, request):
                captured["reference"] = reference
                captured["selection"] = selection
                captured["request"] = request
                return OpenedInput()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "chapter.TXT"
            path.write_text("Source\n", encoding="utf-8")
            with patch(
                "editor_project.create_parser_application_surface",
                return_value=Surface(),
            ):
                project = load_project(path)

        self.assertEqual(project.path, path.absolute())
        self.assertEqual(project.segments[0].target, "")
        self.assertFalse(project.segments[0].confirmed)
        self.assertIs(type(captured["reference"]), SourceReference)
        reference = captured["reference"]
        assert isinstance(reference, SourceReference)
        self.assertEqual(reference.safe_root, str(path.parent))
        self.assertEqual(reference.selected_path, str(path))
        self.assertEqual(captured["selection"].purpose, EffectivePurpose.PROJECT_DOCUMENT)
        self.assertEqual(captured["selection"].format_id, LINE_TEXT_V1)
        self.assertEqual(captured["request"].format_id, LINE_TEXT_V1)

    def test_load_rejects_a_final_component_symlink_instead_of_resolving_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real.json"
            link = root / "linked.json"
            self._write_json(real, [{"source": "Must stay behind the link"}])
            link.symlink_to(real.name)

            with self.assertRaises(ProjectError):
                load_project(link)
            self.assertTrue(link.is_symlink())

    def test_save_emits_complete_v1_schema_in_segment_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "saved.json"
            project = EditorProject(
                name="Saved project",
                source_locale="en-GB",
                target_locale="zh-CN",
                segments=(
                    EditorSegment(
                        id="two",
                        source="Second",
                        target="第二",
                        speaker="B",
                        confirmed=False,
                    ),
                    EditorSegment(
                        id="one",
                        source="First",
                        target="第一",
                        speaker="A",
                        confirmed=True,
                    ),
                ),
            )

            result = save_project(project, path)
            raw_bytes = path.read_bytes()
            payload = json.loads(raw_bytes.decode("utf-8"))

        self.assertEqual(result, path.absolute())
        self.assertTrue(raw_bytes.endswith(b"\n"))
        self.assertEqual(
            list(payload),
            ["schema_version", "name", "source_locale", "target_locale", "segments"],
        )
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(
            [segment["id"] for segment in payload["segments"]],
            ["two", "one"],
        )
        self.assertEqual(
            list(payload["segments"][0]),
            ["id", "source", "target", "speaker", "confirmed"],
        )
        self.assertEqual(payload["segments"][0]["target"], "第二")
        self.assertFalse(payload["segments"][0]["confirmed"])
        self.assertTrue(payload["segments"][1]["confirmed"])

    def test_save_maps_editor_project_to_canonical_dto_and_delegates_writer(self) -> None:
        captured: dict[str, object] = {}

        class Prepared:
            def write(self, target):
                captured["target"] = target
                self.assert_parent_exists(target)
                Path(target.selected_path).write_bytes(b"delegated")
                return object()

            @staticmethod
            def assert_parent_exists(target) -> None:
                if not Path(target.safe_root).is_dir():
                    raise AssertionError("facade did not create the parent after prepare")

        class Surface:
            def prepare_canonical(self, purpose, request):
                captured["purpose"] = purpose
                captured["request"] = request
                if path.parent.exists():
                    raise AssertionError("facade touched the parent before prepare")
                return Prepared()

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing-parent" / "delegated.json"
            project = EditorProject(
                name="Delegated",
                source_locale="en-US",
                target_locale="zh-CN",
                segments=(
                    EditorSegment(
                        id="one",
                        source="Source",
                        target="Target",
                        speaker="Speaker",
                        confirmed=True,
                    ),
                ),
            )

            with patch(
                "editor_project.create_parser_application_surface",
                return_value=Surface(),
            ):
                result = save_project(project, path)

            self.assertEqual(path.read_bytes(), b"delegated")

        self.assertEqual(result, path.absolute())
        self.assertIs(captured["purpose"], EffectivePurpose.PROJECT_DOCUMENT)
        self.assertIs(type(captured["request"]), CanonicalSerializeRequest)
        request = captured["request"]
        assert isinstance(request, CanonicalSerializeRequest)
        self.assertEqual(request.format_id, LOCALCAT_JSON_V1)
        self.assertEqual(request.document.name, "Delegated")
        self.assertEqual(request.document.segments[0].local_id, "one")
        self.assertEqual(request.document.segments[0].speaker.value, "Speaker")
        self.assertTrue(request.document.segments[0].confirmed)
        self.assertIs(type(captured["target"]), TargetReference)
        target = captured["target"]
        assert isinstance(target, TargetReference)
        self.assertEqual(target.safe_root, str(path.parent))
        self.assertEqual(target.selected_path, str(path))

    def test_save_prepare_failures_do_not_create_a_missing_parent_or_target(self) -> None:
        class FailingSurface:
            def __init__(self, code: str) -> None:
                self.code = code

            def prepare_canonical(self, _purpose, _request):
                raise ContractViolation(self.code, "body-safe prepare failure")

        failure_codes = (
            "PARSER.SELECTION.UNSUPPORTED",
            "PARSER.CAPABILITY.WRITE_UNSUPPORTED",
            "PARSER.SELECTION.FACTORY_FAILED",
            "PARSER.SOURCE.WRITE_FAILED",
            "PARSER.SELECTION.FACTORY_MISMATCH",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = EditorProject(
                name="Prepare failure",
                segments=(EditorSegment(id="one", source="Source"),),
            )
            for index, code in enumerate(failure_codes):
                with self.subTest(code=code):
                    parent = root / f"missing-{index}"
                    target = parent / "project.json"
                    with patch(
                        "editor_project.create_parser_application_surface",
                        return_value=FailingSurface(code),
                    ):
                        with self.assertRaises(ProjectError):
                            save_project(project, target)
                    self.assertFalse(parent.exists())
                    self.assertFalse(target.exists())

    def test_save_invalid_neutral_dto_does_not_create_a_missing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "invalid-dto-parent"
            target = parent / "project.json"
            project = EditorProject(
                name="Invalid DTO",
                segments=(
                    EditorSegment(
                        id="one",
                        source="Source",
                        speaker=7,  # type: ignore[arg-type]
                    ),
                ),
            )

            with self.assertRaises(ProjectError):
                save_project(project, target)

            self.assertFalse(parent.exists())
            self.assertFalse(target.exists())

    def test_save_unsupported_suffix_is_rejected_before_prepare_or_mkdir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "unsupported-parent"
            target = parent / "project.txt"
            with patch(
                "editor_project.create_parser_application_surface",
                side_effect=AssertionError("unsupported save reached Parser prepare"),
            ):
                with self.assertRaises(ProjectError):
                    save_project(
                        EditorProject(
                            name="Unsupported",
                            segments=(EditorSegment(id="one", source="Source"),),
                        ),
                        target,
                    )
            self.assertFalse(parent.exists())
            self.assertFalse(target.exists())

    def test_save_rejects_a_final_component_symlink_without_replacing_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real.json"
            link = root / "linked.json"
            original_bytes = b'{"sentinel":"keep"}\n'
            real.write_bytes(original_bytes)
            link.symlink_to(real.name)

            with self.assertRaises(ProjectError):
                save_project(
                    EditorProject(
                        name="Replacement",
                        segments=(EditorSegment(id="one", source="Source"),),
                    ),
                    link,
                )

            self.assertEqual(real.read_bytes(), original_bytes)
            self.assertTrue(link.is_symlink())

    def test_save_rejects_a_parent_symlink_after_successful_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_parent = root / "real-parent"
            linked_parent = root / "linked-parent"
            real_parent.mkdir()
            linked_parent.symlink_to(real_parent.name, target_is_directory=True)
            target = linked_parent / "project.json"

            with self.assertRaises(ProjectError):
                save_project(
                    EditorProject(
                        name="Parent link",
                        segments=(EditorSegment(id="one", source="Source"),),
                    ),
                    target,
                )

            self.assertTrue(linked_parent.is_symlink())
            self.assertFalse((real_parent / "project.json").exists())

    def test_failed_atomic_replace_preserves_target_and_removes_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "protected.json"
            original_bytes = b'{"sentinel":"keep"}\n'
            path.write_bytes(original_bytes)
            project = EditorProject(
                name="Replacement",
                segments=(EditorSegment(id="one", source="Source"),),
            )

            with patch("editor_project.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(ProjectError):
                    save_project(project, path)

            preserved_bytes = path.read_bytes()
            remaining_names = tuple(candidate.name for candidate in root.iterdir())

        self.assertEqual(preserved_bytes, original_bytes)
        self.assertEqual(remaining_names, ("protected.json",))

    def test_controller_installs_only_success_and_clears_dirty_only_after_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = EditorController(ResourceRepository(root / "app-data"))
            valid_path = root / "valid.json"
            invalid_path = root / "invalid.json"
            output_path = root / "output.json"
            self._write_json(
                valid_path,
                {
                    "segments": [
                        {"id": "one", "source": "One"},
                        {"id": "two", "source": "Two"},
                    ]
                },
            )
            invalid_path.write_text('{"segments":[{"source":', encoding="utf-8")

            controller.open_project(valid_path)
            controller.go_to(1)
            controller.update_target("Draft")
            project_before_failure = controller.project
            index_before_failure = controller.current_index
            session_before_failure = controller.project_session_id
            epoch_before_failure = controller.query_epoch

            with self.assertRaisesRegex(
                EditorControllerError,
                r"^PROJECT\.LOAD_FAILED$",
            ):
                controller.open_project(invalid_path)

            self.assertIs(controller.project, project_before_failure)
            self.assertEqual(controller.current_index, index_before_failure)
            self.assertEqual(controller.project_session_id, session_before_failure)
            self.assertEqual(controller.query_epoch, epoch_before_failure)
            self.assertTrue(controller.dirty)

            with self.assertRaisesRegex(
                EditorControllerError,
                r"^PROJECT\.SAVE_FAILED$",
            ):
                controller.save_project(root / "unsupported.txt")

            self.assertIs(controller.project, project_before_failure)
            self.assertTrue(controller.dirty)

            saved = controller.save_project(output_path)

        self.assertEqual(saved.path, output_path.absolute())
        self.assertEqual(saved.segments[1].target, "Draft")
        self.assertFalse(controller.dirty)


if __name__ == "__main__":
    unittest.main()
