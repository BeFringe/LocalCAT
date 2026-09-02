"""WA-07 Windows fresh-process Controller/Feature5 surface acceptance."""

from __future__ import annotations

import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_WORKER = _SOURCE_ROOT / "tests" / "windows_feature5_controller_worker.py"
_PRIVATE_BODY = "PRIVATE_BODY_MUST_NOT_LEAK"


def _command(root: Path, mode: str, *extra: str) -> list[str]:
    return [sys.executable, "-B", str(_WORKER), mode, str(root), *extra]


def _non_repository_cwd(root: Path) -> Path:
    cwd = root.parent / "worker-cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    return cwd


def _run(root: Path, mode: str, *extra: str) -> dict[str, object]:
    completed = subprocess.run(
        _command(root, mode, *extra),
        cwd=_non_repository_cwd(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"worker {mode} exited {completed.returncode}; "
            f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
        )
    if _PRIVATE_BODY in completed.stdout or _PRIVATE_BODY in completed.stderr:
        raise AssertionError("worker leaked private body")
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(f"worker {mode} returned invalid JSON: {lines!r}")
    payload = json.loads(lines[0])
    if type(payload) is not dict:
        raise AssertionError("worker result must be one JSON object")
    return payload


def _terminate(process: subprocess.Popen[bytes], exit_code: int = 96) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int32
    if not terminate(int(process._handle), exit_code):
        raise ctypes.WinError(ctypes.get_last_error())


def _park_and_kill(root: Path, phase: str, marker: Path) -> dict[str, object]:
    process = subprocess.Popen(
        _command(
            root,
            "park",
            "--phase",
            phase,
            "--marker",
            str(marker),
        ),
        cwd=_non_repository_cwd(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 180.0
        while not marker.is_file():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "phase worker exited before marker; "
                    f"code={process.returncode}; stdout={stdout!r}; stderr={stderr!r}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("phase worker marker timeout")
            time.sleep(0.02)
        observed = json.loads(marker.read_text(encoding="utf-8"))
        _terminate(process)
        stdout, stderr = process.communicate(timeout=30.0)
        if process.returncode != 96:
            raise AssertionError(
                f"phase worker exit mismatch: {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        return observed
    finally:
        if process.poll() is None:
            _terminate(process)
            process.communicate(timeout=30.0)


def _statuses(result: dict[str, object]) -> dict[str, dict[str, object]]:
    report = result["report"]
    assert isinstance(report, dict)
    statuses = report["statuses"]
    assert isinstance(statuses, list)
    return {
        str(item["resource_id"]): item
        for item in statuses
        if isinstance(item, dict)
    }


def _status_named(
    result: dict[str, object],
    resource_name: str,
) -> dict[str, object]:
    matches = tuple(
        status
        for status in _statuses(result).values()
        if status["resource_name"] == resource_name
    )
    if len(matches) != 1:
        raise AssertionError(f"missing exact status for {resource_name!r}: {result!r}")
    return matches[0]


def _lifecycle_status_named(
    result: dict[str, object],
    resource_name: str,
) -> dict[str, object]:
    statuses = result["lifecycle_before"]
    assert isinstance(statuses, list)
    matches = tuple(
        status
        for status in statuses
        if isinstance(status, dict)
        and status["resource_name"] == resource_name
    )
    if len(matches) != 1:
        raise AssertionError(
            f"missing lifecycle status for {resource_name!r}: {result!r}"
        )
    return matches[0]


def _targets(result: dict[str, object]) -> list[str]:
    report = result["report"]
    assert isinstance(report, dict)
    suggestions = report["suggestions"]
    assert isinstance(suggestions, list)
    return [
        str(item["target"])
        for item in suggestions
        if isinstance(item, dict)
    ]


def _exact_tree(root: Path) -> tuple[tuple[str, str, bytes | None], ...]:
    facts: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            facts.append((relative, "directory", None))
        elif path.is_file():
            facts.append((relative, "file", path.read_bytes()))
        else:
            facts.append((relative, "other", None))
    return tuple(facts)


def _assert_stable_surface(case: unittest.TestCase, result: dict[str, object]) -> None:
    case.assertTrue(result["repeat_equal"], result)
    case.assertTrue(result["membership_equal"], result)
    case.assertFalse(result["private_body_visible"], result)
    case.assertIn("healthy-peer", _targets(result), result)


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsFeature5FreshProcessTests(unittest.TestCase):
    def test_success_rollback_and_source_divergence_reopen_stable_membership(
        self,
    ) -> None:
        for scenario in ("success", "rollback", "source-diverged"):
            with self.subTest(scenario=scenario):
                temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                base = Path(temporary.name).resolve()
                root = base / scenario
                try:
                    prepared = _run(
                        root,
                        "prepare",
                        "--scenario",
                        "rollback" if scenario == "rollback" else "success",
                    )
                    if scenario == "source-diverged":
                        poisoned = _run(root, "poison-source")
                        self.assertNotEqual(prepared["pid"], poisoned["pid"])
                    result = _run(root, "probe")

                    self.assertNotEqual(prepared["pid"], result["pid"])
                    _assert_stable_surface(self, result)
                    self.assertEqual(result["before"], result["report"])
                    primary = _status_named(result, "Primary TM")
                    healthy = _status_named(result, "Healthy TM")
                    self.assertEqual(healthy["mode"], "LEGACY_EXACT_ONLY")
                    self.assertEqual(healthy["safe_codes"], [])
                    targets = _targets(result)
                    if scenario == "success":
                        self.assertEqual(primary["mode"], "CANONICAL_ACTIVE")
                        self.assertEqual(
                            primary["safe_codes"],
                            [
                                "RETRIEVAL.CONTEXT_EVIDENCE_MISSING",
                                "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_MISSING",
                            ],
                        )
                        self.assertIn("canonical-primary", targets)
                    elif scenario == "rollback":
                        self.assertEqual(
                            primary["mode"],
                            "LEGACY_EXACT_ONLY",
                        )
                        self.assertEqual(primary["safe_codes"], [])
                        self.assertIn("canonical-primary", targets)
                        self.assertEqual(
                            prepared["outcome"],
                            {
                                "kind": "MigrationFailure",
                                "error_code": "MIGRATION.INITIAL_IO_FAILED",
                                "published": False,
                                "ambiguous": False,
                            },
                        )
                        retry = _run(root, "probe-prepare")
                        self.assertEqual(
                            retry["prepare"],
                            {
                                "error_code": None,
                                "issued": True,
                                "prepared_action": "INITIAL",
                            },
                        )
                    else:
                        self.assertEqual(primary["mode"], "SOURCE_DIVERGED")
                        self.assertEqual(
                            primary["safe_codes"],
                            [
                                "TM.RUNTIME.SOURCE_DIVERGED",
                                "RETRIEVAL.CONTEXT_EVIDENCE_MISSING",
                                "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_MISSING",
                            ],
                        )
                        retry = _run(root, "probe-prepare")
                        self.assertEqual(
                            retry["prepare"],
                            {
                                "error_code": "MIGRATION.SIDECAR_DIFFERENT_SOURCE",
                                "issued": False,
                                "prepared_action": None,
                            },
                        )
                        self.assertIn("canonical-primary", targets)
                        self.assertNotIn(_PRIVATE_BODY, targets)
                finally:
                    temporary.cleanup()

    def test_post_generation_tails_recover_or_reopen_canonical(
        self,
    ) -> None:
        cases = (
            ("generation-published", "GENERATION_PUBLISHED", True),
            ("stage-retired", "STAGE_RETIRED", True),
            ("lineage-marker", "LINEAGE_MARKER_PUBLISHED", False),
        )
        for scenario, phase, requires_recovery in cases:
            with self.subTest(scenario=scenario):
                temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                base = Path(temporary.name).resolve()
                root = base / scenario
                marker = base / f"{scenario}.marker.json"
                try:
                    observed = _park_and_kill(root, phase, marker)
                    if not requires_recovery:
                        classification = _run(root, "preflight-pending")
                        self.assertEqual(
                            classification,
                            {
                                "mode": "preflight-pending",
                                "pid": classification["pid"],
                                "error_code": "MIGRATION.RECOVERY_NOT_APPLICABLE",
                                "issued": False,
                            },
                        )
                    result = _run(
                        root,
                        "probe-qt" if requires_recovery else "probe",
                    )

                    self.assertNotEqual(observed["pid"], result["pid"])
                    _assert_stable_surface(self, result)
                    primary = _status_named(result, "Primary TM")
                    healthy = _status_named(result, "Healthy TM")
                    self.assertEqual(healthy["mode"], "LEGACY_EXACT_ONLY")
                    self.assertEqual(healthy["safe_codes"], [])
                    self.assertEqual(primary["mode"], "CANONICAL_ACTIVE")
                    self.assertEqual(
                        primary["safe_codes"],
                        [
                            "RETRIEVAL.CONTEXT_EVIDENCE_MISSING",
                            "RETRIEVAL.FUZZY_CORRECTNESS_EVIDENCE_MISSING",
                        ],
                    )
                    self.assertIn("canonical-primary", _targets(result))
                    if requires_recovery:
                        lifecycle_primary = _lifecycle_status_named(
                            result,
                            "Primary TM",
                        )
                        self.assertEqual(
                            lifecycle_primary["safe_codes"],
                            ["TM.RUNTIME.CANONICAL_RECOVERY_REQUIRED"],
                        )
                        qt = result["qt"]
                        assert isinstance(qt, dict)
                        self.assertTrue(qt["action_enabled"], result)
                        self.assertEqual(qt["action_text"], "恢复 canonical")
                        before = result["before"]
                        after = result["report"]
                        assert isinstance(before, dict)
                        assert isinstance(after, dict)
                        self.assertEqual(
                            result["activation"],
                            {
                                "phase": "COMPLETED",
                                "safe_code": None,
                                "succeeded": True,
                            },
                        )
                        self.assertEqual(after["epoch"], before["epoch"] + 1)
                        self.assertEqual(
                            tuple(
                                item["mode"]
                                for item in before["statuses"]
                                if item["mode"] != "LEGACY_EXACT_ONLY"
                            ),
                            ("UNAVAILABLE",),
                        )
                        self.assertEqual(qt["prompt_count"], 1)
                        self.assertIn("已完成", qt["status_text"])
                    else:
                        self.assertEqual(result["before"], result["report"])
                        self.assertIsNone(result["activation"])
                        self.assertIsNone(result["qt"])
                finally:
                    temporary.cleanup()

    def test_ambiguous_and_private_failure_keep_healthy_peer(
        self,
    ) -> None:
        cases = (
            ("ambiguous", "DB_REPLACED"),
            ("private-failure", None),
        )
        for scenario, phase in cases:
            with self.subTest(scenario=scenario):
                temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                base = Path(temporary.name).resolve()
                root = base / scenario
                marker = base / f"{scenario}.marker.json"
                try:
                    if phase is None:
                        prepared = _run(root, "prepare", "--scenario", "success")
                        creator_pid = prepared["pid"]
                    else:
                        observed = _park_and_kill(root, phase, marker)
                        creator_pid = observed["pid"]
                    if scenario == "private-failure":
                        _ = _run(root, "corrupt-private")
                    result = _run(root, "probe-qt")

                    self.assertNotEqual(creator_pid, result["pid"])
                    _assert_stable_surface(self, result)
                    primary = _status_named(result, "Primary TM")
                    healthy = _status_named(result, "Healthy TM")
                    self.assertEqual(healthy["mode"], "LEGACY_EXACT_ONLY")
                    self.assertEqual(healthy["safe_codes"], [])
                    qt = result["qt"]
                    assert isinstance(qt, dict)
                    lifecycle_primary = _lifecycle_status_named(
                        result,
                        "Primary TM",
                    )
                    self.assertEqual(
                        lifecycle_primary["safe_codes"],
                        ["TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE"],
                    )
                    self.assertFalse(qt["action_enabled"], result)
                    self.assertEqual(qt["action_text"], "Canonical 不可用")
                    self.assertIsNone(result["activation"])
                    self.assertEqual(result["before"], result["report"])
                    self.assertEqual(primary["mode"], "UNAVAILABLE")
                    self.assertEqual(
                        primary["safe_codes"],
                        ["TM.RUNTIME.CANONICAL_AUTHORITY_UNAVAILABLE"],
                    )
                    self.assertEqual(_targets(result), ["healthy-peer"])
                    self.assertEqual(qt["prompt_count"], 0)
                    retry = _run(root, "probe-prepare")
                    self.assertEqual(
                        retry["prepare"],
                        {
                            "error_code": "MIGRATION.RECOVERY_NOT_APPLICABLE",
                            "issued": False,
                            "prepared_action": None,
                        },
                    )
                finally:
                    temporary.cleanup()

    def test_arbitrary_sidecar_without_w1_is_read_only_not_recovery(
        self,
    ) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(temporary.name).resolve()
        root = base / "arbitrary-sidecar"
        try:
            setup = _run(root, "setup-arbitrary-sidecar")
            before = _exact_tree(root)
            self.assertFalse(
                any(
                    relative.endswith("localcat-initial-activation.lock")
                    for relative, _kind, _body in before
                )
            )
            result = _run(root, "preflight-pending")
            after = _exact_tree(root)

            self.assertNotEqual(setup["pid"], result["pid"])
            self.assertEqual(before, after)
            self.assertEqual(
                result,
                {
                    "mode": "preflight-pending",
                    "pid": result["pid"],
                    "error_code": "MIGRATION.RECOVERY_NOT_APPLICABLE",
                    "issued": False,
                },
            )
        finally:
            temporary.cleanup()

    def test_pending_recovery_classification_is_read_only_for_every_tail(
        self,
    ) -> None:
        cases = (
            ("stage-present", "GENERATION_PUBLISHED", True),
            ("stage-retired", "STAGE_RETIRED", True),
            ("ready", "LINEAGE_MARKER_PUBLISHED", False),
            ("db-replaced", "DB_REPLACED", False),
            ("private-tamper", None, False),
        )
        for scenario, phase, recoverable in cases:
            with self.subTest(scenario=scenario):
                temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                base = Path(temporary.name).resolve()
                root = base / scenario
                marker = base / f"{scenario}.marker.json"
                try:
                    if phase is None:
                        creator = _run(root, "prepare", "--scenario", "success")
                        _ = _run(root, "corrupt-private")
                        creator_pid = creator["pid"]
                    else:
                        observed = _park_and_kill(root, phase, marker)
                        creator_pid = observed["pid"]
                    before = _exact_tree(root)

                    result = _run(root, "preflight-pending")
                    after = _exact_tree(root)

                    self.assertNotEqual(creator_pid, result["pid"])
                    self.assertEqual(after, before)
                    self.assertEqual(
                        result,
                        {
                            "mode": "preflight-pending",
                            "pid": result["pid"],
                            "error_code": (
                                None
                                if recoverable
                                else "MIGRATION.RECOVERY_NOT_APPLICABLE"
                            ),
                            "issued": recoverable,
                        },
                    )
                finally:
                    temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
