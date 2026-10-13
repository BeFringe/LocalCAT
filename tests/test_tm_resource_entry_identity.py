"""正常资源入口在恢复前核对调用方身份，并保留原家族。"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import tm_engine
from tm_contracts import CanonicalResourceIdentity, MigrationReport
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.iterdir()
        if path.is_file()
    }


@contextmanager
def _factory_entries():
    calls = dict(transition=0, reserved=0, facts=0, rehydrate=0)

    def recording(name, original):
        def invoke(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return invoke

    with ExitStack() as stack:
        for owner, attribute, name in (
            (ResourceStoreCoordinator, "_family_transition", "transition"),
            (tm_engine, "_open_canonical_tm_store_reserved", "reserved"),
            (tm_engine, "_activation_facts", "facts"),
            (ResourceStoreCoordinator, "rehydrate_runtime_authority", "rehydrate"),
        ):
            stack.enter_context(
                patch.object(owner, attribute, recording(name, getattr(owner, attribute)))
            )
        yield calls


def _cold_wrong_identity(path: Path) -> None:
    """全新解释器只观察真实公开 factory；不替换其身份或恢复行为。"""
    assert tm_engine._known_resource_family_identity(path) is None
    before = _file_hashes(path.parent)
    with _factory_entries() as calls:
        try:
            store = tm_engine.open_canonical_tm_store(
                path, expected_resource_id="resource.wrong"
            )
        except ValueError as error:
            code = str(error)
        else:
            if store is not None:
                store.coordinator.close_family_session()
            raise AssertionError("cold wrong-id factory must reject")
    assert code == "TM.CANONICAL_UNHEALTHY", code
    assert calls == dict(transition=1, reserved=0, facts=0, rehydrate=0), calls
    assert _file_hashes(path.parent) == before
    print(json.dumps({"code": code, "calls": calls, "files_unchanged": True}))


@unittest.skipIf(sys.platform == "win32", "POSIX 原资源家族入口")
class TMResourceEntryIdentityTests(unittest.TestCase):
    def __init__(self, methodName: str = "runTest") -> None:
        super().__init__(methodName)
        self.root: Path
        self.path: Path
        self.identity: CanonicalResourceIdentity
        self.owner: ResourceStoreCoordinator

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # Keep the ordinary configured spelling, including macOS /var aliases.
        self.root = Path(temporary.name)
        self.path = self.root / "memory.jsonl"
        self.path.write_text(
            '{"source":"hello","target":"stable"}\n', encoding="utf-8"
        )
        self.identity = CanonicalResourceIdentity.from_configured_jsonl(
            "resource.correct", self.path
        )
        self.owner = ResourceStoreCoordinator(
            resource_identity=self.identity, canonical_store_id="store.correct"
        )
        self.addCleanup(self.owner.close_family_session)
        service = TMMigrationService(
            resource_identity=self.identity,
            canonical_store_id="store.correct",
            coordinator=self.owner,
        )
        self.assertIs(
            type(service.activate_initial(self.path, self.identity.resource_id)),
            MigrationReport,
        )

    def test_known_family_wrong_identity_stops_before_transition_and_recovery(self) -> None:
        before = _file_hashes(self.root)
        original_family = self.owner._family
        with _factory_entries() as calls:
            with self.assertRaisesRegex(ValueError, "^TM.CANONICAL_UNHEALTHY$"):
                tm_engine.open_canonical_tm_store(
                    self.path, expected_resource_id="resource.wrong"
                )
        self.assertEqual(calls, dict(transition=0, reserved=0, facts=0, rehydrate=0))
        self.assertEqual(_file_hashes(self.root), before)
        self.assertIs(self.owner._family, original_family)
        self.assertEqual(self.owner.state, "READY")

    def test_known_family_correct_identity_rehydrates_the_original_family(self) -> None:
        with _factory_entries() as calls:
            store = tm_engine.open_canonical_tm_store(
                self.path, expected_resource_id=self.identity.resource_id
            )
        self.assertIsNotNone(store)
        assert store is not None
        self.addCleanup(store.coordinator.close_family_session)
        self.assertEqual(calls, dict(transition=1, reserved=1, facts=1, rehydrate=1))
        self.assertEqual(store.resource_id, self.identity.resource_id)
        self.assertIs(store.coordinator._family, self.owner._family)
        self.assertEqual(store.canonical_revision().record_count, 1)

    def test_cold_wrong_identity_stops_before_reserved_recovery_without_file_changes(self) -> None:
        before = _file_hashes(self.root)
        child = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from pathlib import Path; import sys; "
                "from tests.test_tm_resource_entry_identity import _cold_wrong_identity; "
                "_cold_wrong_identity(Path(sys.argv[1]))",
                str(self.path),
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        self.assertEqual(_file_hashes(self.root), before)


if __name__ == "__main__":
    unittest.main()
