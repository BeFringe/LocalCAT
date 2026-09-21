"""ProjectPackage suggestion commits must finish without weakening generation guards."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from threading import Event, Thread
import unittest
from unittest.mock import patch

from capability_host import compose_capability_host
from editor_controller import EditorControllerError
from tests.benchmark_worker_test_support import worker_temporary_directory
from tests.source_authority_support import current_source_authority
from tests.test_editor_controller_tm_apply import (
    _canonical_controller,
    _legacy_fixture,
)


def _exercise_workspace_apply(mode: str, temporary: str) -> None:
    case = unittest.TestCase()
    root = Path(temporary)
    if mode == "canonical":
        controller, runtime, composition = _canonical_controller(case, root)
    else:
        composition = compose_capability_host(
            source_authority=current_source_authority(),
            evaluated_at_utc=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
        )
        controller, _, runtime, _ = _legacy_fixture(
            root, capability_host=composition.host,
        )
    segment = controller.current_segment
    source_root = root / "sources"
    source_root.mkdir()
    paths = []
    for index in range(2):
        path = source_root / f"chapter-{index}.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "name": f"Chapter {index}",
            "source_locale": "en", "target_locale": "zh-CN",
            "segments": [{"id": "one", "source": segment.source,
                          "target": "before", "speaker": segment.speaker or "",
                          "confirmed": True}],
        }, ensure_ascii=False), encoding="utf-8")
        paths.append(path)
    package = root / "project.localcat-project"
    controller.create_workspace_package(
        source_root, tuple(paths), package,
        name="Workspace apply", source_locale="en", target_locale="zh-CN",
    )
    original_bytes = {path: path.read_bytes() for path in (*paths, package)}

    def issue():
        if mode == "bridge":
            return controller.suggestions().tm_matches[0]
        return controller.tm_suggestion_report().suggestions[0]

    suggestion = issue()
    if mode in ("runtime", "capability"):
        _assert_refresh_waits_for_commit(case, controller, runtime,
                                        composition, suggestion, mode)
    else:
        revision = controller.project_revision
        controller.apply_tm_suggestion(suggestion)
        case.assertEqual(controller.current_segment.target, suggestion.target)
        case.assertFalse(controller.current_segment.confirmed)
        case.assertEqual(controller.project_revision, revision + 1)
        with case.assertRaises(EditorControllerError):
            controller.apply_tm_suggestion(suggestion)

        fresh = issue()
        revision = controller.project_revision
        controller.apply_tm_suggestion(fresh)
        case.assertEqual(controller.project_revision, revision)  # no-op
        controller.apply_tm_suggestion(fresh)

        controller.update_workspace_target("direct edit")
        with case.assertRaises(EditorControllerError):
            controller.apply_tm_suggestion(fresh)
        fresh = issue()
        runtime.refresh(controller.repository.list_resources())
        with case.assertRaises(EditorControllerError):
            controller.apply_tm_suggestion(fresh)
        controller.apply_tm_suggestion(issue())
        case.assertEqual(controller.current_segment.target, suggestion.target)

    case.assertEqual({path: path.read_bytes() for path in original_bytes},
                     original_bytes)
    controller.save_workspace_package()
    controller.open_project_package(package)
    case.assertEqual(controller.current_segment.target, suggestion.target)
    case.assertFalse(controller.current_segment.confirmed)
    case.assertEqual([path.read_bytes() for path in paths],
                     [original_bytes[path] for path in paths])


def _assert_refresh_waits_for_commit(case, controller, runtime, composition,
                                   suggestion, trigger):
    committed = Event()
    release = Event()
    refresh_started = Event()
    refresh_finished = Event()
    snapshot_captured = Event()
    errors = []
    original = controller.update_workspace_target
    host = composition.host
    original_snapshot = host.retrieval_snapshot
    initial_generation = original_snapshot().generation

    def observed_snapshot(observed_host):
        case.assertIs(observed_host, host)
        refresh_started.set()
        snapshot = original_snapshot()
        snapshot_captured.set()
        return snapshot

    def paused_update(target):
        result = original(target)
        committed.set()
        if not release.wait(5):
            raise AssertionError("commit release was not signalled")
        return result

    def apply():
        try:
            controller.apply_tm_suggestion(suggestion)
        except BaseException as error:
            errors.append(error)

    def refresh():
        try:
            if trigger == "runtime":
                refresh_started.set()
                runtime.refresh(controller.repository.list_resources())
            else:
                owner = composition.retrieval_gate_c_validation_owner
                with patch.object(type(host), "retrieval_snapshot", new=observed_snapshot):
                    owner.validate_gate_c(
                        generated_at_utc=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                        valid_until_utc=datetime(2030, 1, 2, 10, tzinfo=timezone.utc),
                        evaluated_at_utc=datetime(2030, 1, 1, 12, tzinfo=timezone.utc),
                    )
        except BaseException as error:
            errors.append(error)
        finally:
            refresh_finished.set()

    with patch.object(controller, "update_workspace_target", side_effect=paused_update):
        applying = Thread(target=apply, daemon=True)
        refreshing = Thread(target=refresh, daemon=True)
        applying.start()
        try:
            case.assertTrue(committed.wait(5), "workspace commit did not finish")
            refreshing.start()
            case.assertTrue(refresh_started.wait(5), repr(errors))
            if trigger == "capability":
                case.assertFalse(snapshot_captured.wait(0.1))
            else:
                case.assertFalse(refresh_finished.wait(0.1))
        finally:
            release.set()
            applying.join(5)
            if refreshing.ident is not None:
                refreshing.join(5)
        case.assertFalse(applying.is_alive())
        case.assertFalse(refreshing.is_alive())
        case.assertEqual(errors, [])
        case.assertEqual(controller.current_segment.target, suggestion.target)
        if trigger == "capability":
            case.assertTrue(snapshot_captured.is_set())
            case.assertEqual(original_snapshot().generation, initial_generation + 1)


class WorkspaceTMApplyLivenessTests(unittest.TestCase):
    def _run_case(self, mode):
        # Clean fixtures after the child releases its file authorities.
        with worker_temporary_directory() as temporary:
            self._run_child(mode, temporary)

    def _run_child(self, mode, temporary):
        # A real deadlock must fail within a bound, without hanging the test runner.
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-c",
                 "import faulthandler; faulthandler.dump_traceback_later(20); "
                 "from tests.test_workspace_tm_apply_liveness import _exercise_workspace_apply; "
                 f"_exercise_workspace_apply({mode!r}, {temporary!r}); "
                 "faulthandler.cancel_dump_traceback_later()"],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        except subprocess.TimeoutExpired as error:
            diagnostic = error.stderr or b""
            if isinstance(diagnostic, bytes):
                diagnostic = diagnostic.decode("utf-8", errors="replace")
            self.fail(f"workspace apply timed out: {diagnostic}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_canonical_workspace_apply_completes_and_reopens(self):
        self._run_case("canonical")

    def test_legacy_resource_workspace_apply_completes_and_reopens(self):
        self._run_case("legacy")

    def test_legacy_bridge_workspace_apply_completes_and_reopens(self):
        self._run_case("bridge")

    def test_workspace_commit_blocks_runtime_publication(self):
        self._run_case("runtime")

    def test_workspace_commit_blocks_capability_publication(self):
        self._run_case("capability")


if __name__ == "__main__":
    unittest.main()
