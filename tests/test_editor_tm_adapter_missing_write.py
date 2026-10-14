"""Configured unavailable TM writes must remain visible in confirmation results."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capability_host import CapabilityHost
from editor_contracts import (
    ConfirmResult,
    EditorProject,
    EditorSegment,
    ResourceConfig,
    ResourceKind,
)
from editor_controller import EditorController
from editor_tm_adapter import EditorTMAdapter
from resource_repository import ResourceRepository
from tm_application_composition import LegacyExactPort, TMResourceResolver, TMRuntimeHost
from tm_contracts import TMRecordDraft


def _adapter(runtime: TMRuntimeHost) -> EditorTMAdapter:
    return EditorTMAdapter(
        runtime_host=runtime,
        capability_host=CapabilityHost(
            evaluated_at_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
        ),
    )


class EditorTMAdapterMissingWriteTests(unittest.TestCase):
    def test_cold_start_missing_target_and_flag_controls(self) -> None:
        for missing, active, update in (
            (True, True, True),
            (True, False, True),
            (True, True, False),
            (False, True, True),
        ):
            with self.subTest(missing=missing, active=active, update=update):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    source = root / "configured.jsonl"
                    _ = source.write_text('{"source":"Hello","target":"old"}\n', encoding="utf-8")
                    repository = ResourceRepository(root / "app", default_tm_path=source)
                    _ = repository.update_resource(replace(
                        repository.get("local-tm"), active=active, lookup=False, update=update,
                    ))
                    peer = repository.create_resource("Healthy peer", ResourceKind.TRANSLATION_MEMORY)
                    _ = peer.path.write_text('{"source":"Hello","target":"peer"}\n', encoding="utf-8")
                    if missing:
                        # No live runtime or Core owner exists before this ordinary cold open.
                        source.unlink()
                    repository = ResourceRepository(root / "app")
                    runtime = TMRuntimeHost(resolver=TMResourceResolver(), configs=repository.list_resources())
                    controller = EditorController(repository, tm_adapter=_adapter(runtime))
                    _ = controller.set_project(EditorProject(name="Cold project", segments=(
                        EditorSegment(id="first", source="Hello", target="new target"),
                        EditorSegment(id="second", source="Next"),
                    )))

                    result = controller.confirm_current()
                    assert isinstance(result, ConfirmResult)

                    failed = missing and active and update
                    self.assertEqual(result.write_report.succeeded, not failed)
                    self.assertEqual(result.project.segments[0].confirmed, not failed)
                    self.assertEqual(result.current_index, 0 if failed else 1)
                    written = (peer.id,) if missing else ("local-tm", peer.id)
                    self.assertEqual(result.write_report.written_resource_ids, written)
                    self.assertEqual(source.exists(), not missing)
                    self.assertEqual(json.loads(peer.path.read_text(encoding="utf-8").splitlines()[-1])["target"], "new target")
                    if failed:
                        status = runtime.snapshot().statuses[0]
                        outcome = result.write_report.outcomes[0]
                        self.assertEqual(outcome.resource_id, "local-tm")
                        self.assertEqual(outcome.error_code, "TM.RUNTIME.PATH_UNAVAILABLE")
                        self.assertEqual(outcome.error_code, status.safe_codes[0])
                        self.assertEqual(outcome.retryable, status.retryable)
                        self.assertEqual(result.write_report.errors, (outcome.error_code,))
                    else:
                        self.assertEqual(result.write_report.errors, ())

    def test_missing_target_and_peers_keep_captured_order_and_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            configs = tuple(
                ResourceConfig(
                    id=identity, name=identity, kind=kind, path=root / f"{identity}.jsonl",
                    active=active, lookup=False, update=update,
                )
                for identity, kind, active, update in (
                    ("peer.before", ResourceKind.TRANSLATION_MEMORY, True, True),
                    ("missing.selected", ResourceKind.TRANSLATION_MEMORY, True, True),
                    ("terms", ResourceKind.TERMBASE, True, True),
                    ("missing.inactive", ResourceKind.TRANSLATION_MEMORY, False, True),
                    ("missing.no-update", ResourceKind.TRANSLATION_MEMORY, True, False),
                    ("peer.after", ResourceKind.TRANSLATION_MEMORY, True, True),
                )
            )
            for config in (configs[0], configs[-1]):
                _ = config.path.write_text('{"source":"old","target":"old"}\n', encoding="utf-8")
            runtime = TMRuntimeHost(resolver=TMResourceResolver(), configs=configs)
            adapter = _adapter(runtime)
            original_append = LegacyExactPort.append
            calls: list[str] = []

            def append_and_refresh(port: LegacyExactPort, draft: TMRecordDraft) -> None:
                calls.append(port.resource_id)
                if len(calls) == 1:
                    _ = runtime.refresh(tuple(
                        replace(config, name=f"new {config.name}", active=True,
                                update=config.id == "missing.inactive")
                        for config in reversed(configs)
                    ))
                original_append(port, draft)

            with patch.object(LegacyExactPort, "append", append_and_refresh):
                report = adapter.append_confirmed(
                    segment=EditorSegment(id="first", source="Hello"),
                    target="new target", file_source="project.json",
                )

            self.assertEqual(runtime.snapshot().generation, 1)
            self.assertEqual(calls, ["peer.before", "peer.after"])
            self.assertEqual(report.written_resource_ids, ("peer.before", "peer.after"))
            self.assertEqual(report.errors, ("TM.RUNTIME.PATH_UNAVAILABLE",))
            self.assertEqual(tuple((o.resource_id, o.resource_name, o.global_order, o.written)
                                   for o in report.outcomes), (
                ("peer.before", "peer.before", 0, True),
                ("missing.selected", "missing.selected", 1, False),
                ("peer.after", "peer.after", 5, True),
            ))
            for config in (configs[0], configs[-1]):
                self.assertEqual(json.loads(config.path.read_text(encoding="utf-8").splitlines()[-1])["target"], "new target")
            for config in configs[1:-1]:
                self.assertFalse(config.path.exists())

            next_report = adapter.append_confirmed(
                segment=EditorSegment(id="first", source="Hello"),
                target="next target", file_source="project.json",
            )
            self.assertEqual(next_report.written_resource_ids, ())
            self.assertEqual(tuple((o.resource_id, o.resource_name, o.global_order)
                                   for o in next_report.outcomes), (
                ("missing.inactive", "new missing.inactive", 2),
            ))
            self.assertFalse(next_report.succeeded)


if __name__ == "__main__":
    _ = unittest.main()
