from __future__ import annotations

import json
import ctypes
import functools
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import tools.validate_windows_release as evidence_module
import tools.anchor_windows_evidence_harness_smoke as anchor_module
import tools.run_windows_evidence_harness_smoke as smoke_module
from tools.validate_windows_release import (
    EvidenceHarness,
    EvidenceValidationError,
    environment_profile_digest,
    environment_profile_projection,
    validate_evidence_bundle,
)


COMMIT = "a" * 40


def shell_flavor() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        raise RuntimeError("PowerShell is unavailable")
    return (
        "WINDOWS_POWERSHELL"
        if Path(executable).name.casefold() == "powershell.exe"
        else "POWERSHELL_CORE"
    )


@functools.cache
def shell_version() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if executable is None:
        raise RuntimeError("PowerShell is unavailable")
    return subprocess.check_output(
        [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ],
        text=True,
    ).strip()


def command_expectation(
    command_id: str,
    command: str,
    *,
    status: str = "PASS",
    exit_code: int | None = 0,
    diagnostic_code: str | None = None,
    windowed: bool = False,
    cwd_role: str = "NON_REPOSITORY_CWD",
    environment_profile: str = "CLEAN_WINDOWS_V2",
    environment_projection_additions: dict[str, str] | None = None,
) -> dict[str, object]:
    expectation = {
        "id": command_id,
        "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
        "shell_flavor": (
            "WINDOWS_POWERSHELL"
            if environment_profile == "CLEAN_WINDOWS_V2"
            else shell_flavor()
        ),
        "shell_version": (
            "5.1" if environment_profile == "CLEAN_WINDOWS_V2" else shell_version()
        ),
        "cwd_role": cwd_role,
        "environment_profile": environment_profile,
        "environment_profile_sha256": environment_profile_digest(environment_profile),
        "environment_projection": environment_profile_projection(
            environment_profile,
            environment_projection_additions,
        ),
        "windowed": windowed,
        "status": status,
        "exit_code": exit_code,
        "diagnostic_code": diagnostic_code,
    }
    expectation.update(
        {
            "shell_edition": "Desktop",
            "shell_process_architecture": "X64",
            "shell_selection_role": "SYSTEM32_ABSOLUTE",
        }
    )
    return expectation


def environment() -> dict[str, str]:
    return {
        "os_name": "Windows",
        "os_version": "11",
        "os_build": "test-build",
        "architecture": "AMD64",
        "python": "3.14.test",
        "pyside6": "6.11.1",
        "qt": "6.11.1",
        "pyinstaller": "6.22.2",
        "sqlite": "3.test",
        "filesystem": "NTFS",
        "volume_class": "fixed-local",
    }


class WindowsReleaseEvidenceTest(unittest.TestCase):
    def test_clean_windows_v2_records_servicing_revision_without_pin_failure(self) -> None:
        self.assertEqual(
            evidence_module._parse_windows_powershell_probe(
                "Desktop|5.1.26100.9168|True|X64"
            ),
            ("Desktop", "5.1.26100.9168", "X64"),
        )
        self.assertTrue(
            evidence_module._shell_version_matches_expectation(
                "5.1.26100.9168", "5.1", "CLEAN_WINDOWS_V2"
            )
        )
        self.assertFalse(
            evidence_module._shell_version_matches_expectation(
                "7.5.9", "5.1", "CLEAN_WINDOWS_V2"
            )
        )

    def test_clean_windows_v2_rejects_wrong_edition_or_architecture(self) -> None:
        for probe in (
            "Core|5.1.26100.9168|True|X64",
            "Desktop|5.1.26100.9168|False|X64",
            "Desktop|7.5.9.0|True|X64",
            "Desktop|5.1|True|X64",
            "Desktop|5.1.26100|True|X64",
            "Desktop|5.1.26100.9168|True|Arm64",
        ):
            with self.subTest(probe=probe):
                with self.assertRaises(EvidenceValidationError):
                    evidence_module._parse_windows_powershell_probe(probe)

    def test_tracked_task1_scenario_contracts_are_canonical_and_valid(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        contract_root = repository / "packaging/windows/evidence-scenarios"
        expected = {
            "evidence-harness-success.json",
            "evidence-harness-expected-failure.json",
            "evidence-harness-interrupted.json",
        }
        self.assertEqual({path.name for path in contract_root.glob("*.json")}, expected)
        for name in sorted(expected):
            path = contract_root / name
            value = json.loads(path.read_text(encoding="utf-8"))
            evidence_module._validate_scenario_contract(value)
            self.assertEqual(path.read_bytes(), evidence_module._canonical_json_bytes(value))

    def test_tracked_task1_contracts_match_the_smoke_producer(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        for lane_name, lane in sorted(smoke_module.LANES.items()):
            contract_path = repository.joinpath(*str(lane["contract"]).split("/"))
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            command = contract["scenarios"][0]["commands"][0]
            self.assertEqual(command["id"], lane["command_id"], lane_name)
            self.assertEqual(
                command["command_sha256"],
                hashlib.sha256(str(lane["command"]).encode("utf-8")).hexdigest(),
                lane_name,
            )
            self.assertEqual(command["windowed"], lane["windowed"], lane_name)
            self.assertEqual(command["environment_profile"], "CLEAN_WINDOWS_V2")
            self.assertEqual(command["shell_flavor"], "WINDOWS_POWERSHELL")
            self.assertEqual(command["shell_version"], "5.1")
            self.assertEqual(command["shell_edition"], "Desktop")
            self.assertEqual(command["shell_process_architecture"], "X64")
            self.assertEqual(command["shell_selection_role"], "SYSTEM32_ABSOLUTE")

    def test_clean_windows_v1_keeps_exact_version_matching(self) -> None:
        self.assertTrue(
            evidence_module._shell_version_matches_expectation(
                "5.1.26100.8875",
                "5.1.26100.8875",
                "CLEAN_WINDOWS_V1",
            )
        )
        self.assertFalse(
            evidence_module._shell_version_matches_expectation(
                "5.1.26100.9168",
                "5.1.26100.8875",
                "CLEAN_WINDOWS_V1",
            )
        )

    def test_v1_scenario_contract_remains_read_only_compatible(self) -> None:
        expectation = command_expectation(
            "legacy",
            "Write-Output 'LEGACY'",
            environment_profile="CLEAN_WINDOWS_V1",
        )
        for key in (
            "shell_edition",
            "shell_process_architecture",
            "shell_selection_role",
        ):
            expectation.pop(key)
        evidence_module._validate_scenario_contract(
            {
                "schema": evidence_module.SCENARIO_CONTRACT_SCHEMA_ID_V1,
                "id": "legacy-v1",
                "scenarios": [
                    {
                        "id": "legacy",
                        "required": True,
                        "expected_observation": "legacy-contract",
                        "commands": [expectation],
                        "events": [],
                    }
                ],
            }
        )

    def test_clean_windows_v2_matrix_accepts_recorded_servicing_revision(self) -> None:
        expectation = command_expectation("success", "Write-Output 'SAFE'")
        actual = {
            **expectation,
            "shell_version": "5.1.26100.9168",
            "shell_binary_sha256": "b" * 64,
        }
        matrix, status = evidence_module._derive_release_matrix(
            {
                "schema": evidence_module.SCENARIO_CONTRACT_SCHEMA_ID,
                "id": "v2-servicing",
                "scenarios": [
                    {
                        "id": "success",
                        "required": True,
                        "expected_observation": "servicing-recorded",
                        "commands": [expectation],
                        "events": [],
                    }
                ],
            },
            [actual],
            [],
        )
        self.assertEqual(status, "PASS")
        self.assertEqual(matrix["scenarios"][0]["verdict"], "PASS")

    def test_each_command_reproves_shell_identity_in_the_launched_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command = "Write-Output 'SAFE'"
            harness = self.harness(
                root,
                commands=[command_expectation("success", command)],
            )
            harness.powershell_version = "5.1.0.0"
            harness.powershell_edition = "STALE"
            harness.powershell_process_architecture = "STALE"
            harness.powershell_binary_sha256 = "0" * 64

            recorded = harness.run_command("success", command)

            self.assertEqual(recorded["status"], "PASS")
            self.assertEqual(recorded["shell_edition"], "Desktop")
            self.assertEqual(recorded["shell_process_architecture"], "X64")
            self.assertRegex(str(recorded["shell_version"]), r"^5\.1\.\d+\.\d+$")
            self.assertNotEqual(recorded["shell_binary_sha256"], "0" * 64)

    def test_missing_per_command_shell_identity_is_a_harness_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command = "Write-Output 'SAFE'"
            harness = self.harness(
                root,
                commands=[command_expectation("success", command)],
            )
            with mock.patch.object(
                evidence_module,
                "_powershell_identity_prelude",
                return_value=command,
            ):
                recorded = harness.run_command("success", command)

            self.assertEqual(recorded["status"], "FAIL")
            self.assertIsNone(recorded["exit_code"])
            self.assertEqual(
                recorded["diagnostic_code"],
                "EVIDENCE.COMMAND.SHELL_IDENTITY_UNAVAILABLE",
            )
            self.assertEqual(recorded["shell_version"], "UNPROVEN")

    def test_interrupted_command_can_use_digest_matched_initial_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command = "Start-Sleep -Seconds 5"
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "timeout",
                        command,
                        status="INTERRUPTED",
                        exit_code=None,
                        diagnostic_code="EVIDENCE.COMMAND.TIMEOUT",
                    )
                ],
            )
            with mock.patch.object(
                evidence_module,
                "_powershell_identity_prelude",
                return_value=command,
            ):
                recorded = harness.run_command(
                    "timeout",
                    command,
                    timeout_seconds=0.1,
                )

            self.assertEqual(recorded["status"], "INTERRUPTED")
            self.assertEqual(recorded["diagnostic_code"], "EVIDENCE.COMMAND.TIMEOUT")
            self.assertRegex(str(recorded["shell_version"]), r"^5\.1\.\d+\.\d+$")

    def test_v1_contract_is_validation_only_and_producer_fails_early(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            root = Path(directory) / "evidence"
            contract_key = "packaging/windows/evidence-scenarios/legacy.json"
            contract_path = repository.joinpath(*contract_key.split("/"))
            contract_path.parent.mkdir(parents=True)
            expectation = command_expectation(
                "legacy",
                "Write-Output 'LEGACY'",
                environment_profile="CLEAN_WINDOWS_V1",
            )
            for key in (
                "shell_edition",
                "shell_process_architecture",
                "shell_selection_role",
            ):
                expectation.pop(key)
            contract_path.write_bytes(
                evidence_module._canonical_json_bytes(
                    {
                        "schema": evidence_module.SCENARIO_CONTRACT_SCHEMA_ID_V1,
                        "id": "legacy-v1",
                        "scenarios": [
                            {
                                "id": "legacy",
                                "required": True,
                                "expected_observation": "legacy-contract",
                                "commands": [expectation],
                                "events": [],
                            }
                        ],
                    }
                )
            )
            with self.assertRaisesRegex(EvidenceValidationError, "validation-only"):
                EvidenceHarness(
                    root,
                    repository_commit=COMMIT,
                    repository_branch="legacy-v1",
                    run_id="legacy-v1",
                    environment=environment(),
                    repository_root=repository,
                    scenario_contract_key=contract_key,
                )

    def test_external_anchor_context_requires_clean_tracked_head_bytes(self) -> None:
        source_repository = Path(__file__).resolve().parents[1]
        contract_key = str(smoke_module.LANES["success"]["contract"])
        tracked_keys = (
            contract_key,
            "tools/run_windows_evidence_harness_smoke.py",
            "tools/anchor_windows_evidence_harness_smoke.py",
            "tools/validate_windows_release.py",
        )
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            for key in tracked_keys:
                destination = repository.joinpath(*key.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_repository.joinpath(*key.split("/")), destination)
            subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "core.autocrlf", "false"],
                cwd=repository,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "--", *tracked_keys], cwd=repository, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=LocalCAT Test",
                    "-c",
                    "user.email=localcat-test.invalid@example.invalid",
                    "commit",
                    "-m",
                    "test anchor",
                ],
                cwd=repository,
                check=True,
                capture_output=True,
            )

            commit, branch, contract_sha256 = anchor_module._clean_head_context(
                repository, contract_key
            )
            self.assertRegex(commit, r"[0-9a-f]{40}")
            self.assertTrue(branch)
            self.assertEqual(
                contract_sha256,
                hashlib.sha256(repository.joinpath(*contract_key.split("/")).read_bytes()).hexdigest(),
            )

            (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "not clean"):
                anchor_module._clean_head_context(repository, contract_key)

    def harness(
        self,
        root: Path,
        *,
        commands: list[dict[str, object]] | None = None,
        events: list[dict[str, object]] | None = None,
        scenarios: list[dict[str, object]] | None = None,
        private_values: dict[str, str] | None = None,
        repository_branch: str = "codex/windows-platform-enablement",
        run_id: str = "test-run-01",
        environment_override: dict[str, str] | None = None,
        approved_ci_artifact_key: str | None = None,
    ) -> EvidenceHarness:
        contract_key = (
            "packaging/windows/evidence-scenarios/"
            f"{root.name}-scenario-contract.json"
        )
        contract_path = root.parent.joinpath(*contract_key.split("/"))
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        default_scenarios = [
            {
                "id": "test-scenario",
                "required": True,
                "expected_observation": "test-contract",
                "commands": commands
                or [command_expectation("success", "Write-Output 'SAFE'")],
                "events": events or [],
            }
        ]
        contract_path.write_bytes(
            (
                json.dumps(
                    {
                        "schema": evidence_module.SCENARIO_CONTRACT_SCHEMA_ID,
                        "id": "test-contract-v2",
                        "scenarios": scenarios or default_scenarios,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        return EvidenceHarness(
            root,
            repository_commit=COMMIT,
            repository_branch=repository_branch,
            run_id=run_id,
            environment=environment_override or environment(),
            repository_root=root.parent,
            scenario_contract_key=contract_key,
            private_values=private_values,
            approved_ci_artifact_key=approved_ci_artifact_key,
        )

    def validate(
        self,
        root: Path,
        harness: EvidenceHarness,
        *,
        expected_manifest_sha256: str | None = None,
    ) -> dict[str, object]:
        return validate_evidence_bundle(
            root,
            repository_root=harness.repository_root,
            scenario_contract_key=harness.scenario_contract_key,
            expected_repository_commit=harness.repository_commit,
            expected_scenario_contract_sha256=harness.scenario_contract_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
        )

    def register_outputs(
        self,
        root: Path,
        harness: EvidenceHarness,
    ) -> None:
        harness.write_release_matrix()
        (root / "windows-fs-lock-adaptation-checklist.md").write_text(
            "# Windows FS/lock checklist\n",
            encoding="utf-8",
        )
        (root / "frozen-source-packaging-checklist.md").write_text(
            "# Frozen-source packaging checklist\n",
            encoding="utf-8",
        )
        harness.register_release_outputs(
            matrix="matrix.json",
            windows_fs_lock_checklist="windows-fs-lock-adaptation-checklist.md",
            frozen_source_packaging_checklist="frozen-source-packaging-checklist.md",
        )

    def test_success_failure_and_interruption_are_portable_and_valid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            private_root = str(Path(directory).resolve())
            secret = "body-secret-value"
            success_text = "Write-Output 'SAFE_MARKER'"
            failure_text = (
                "Write-Output $env:LOCALCAT_TEST_SECRET; "
                "[Console]::Error.WriteLine($env:LOCALCAT_TEST_ROOT); exit 3"
            )
            timeout_text = "Write-Output 'STARTED'; Start-Sleep -Seconds 5"
            event_expectation = {
                "operation_id": "lock-observation-01",
                "command_id": None,
                "operation": "lock",
                "profile": "LOCK",
                "phase": "acquire",
                "result": "OBSERVED",
                "stable_code": None,
                "win32_code": 33,
                "filesystem": "NTFS",
                "volume_class": "fixed-local",
                "identity_comparison": "MATCH",
                "final_path_verdict": "MATCH",
                "reparse_verdict": "CLEAR",
                "share_flags": ["READ", "WRITE"],
                "lock_range": {"offset": 0, "length": 1},
                "recovery_result": "NOT_APPLICABLE",
            }
            harness = self.harness(
                root,
                commands=[
                    command_expectation("success", success_text),
                    command_expectation(
                        "failure",
                        failure_text,
                        status="FAIL",
                        exit_code=3,
                        environment_projection_additions={
                            "LOCALCAT_TEST_ROOT": "<redacted:repository-root>",
                            "LOCALCAT_TEST_SECRET": "<redacted:body>",
                        },
                    ),
                    command_expectation(
                        "timeout",
                        timeout_text,
                        status="INTERRUPTED",
                        exit_code=None,
                        diagnostic_code="EVIDENCE.COMMAND.TIMEOUT",
                        windowed=True,
                    ),
                ],
                events=[event_expectation],
                private_values={"source-root": private_root, "body": secret},
            )
            passed = harness.run_command(
                "success",
                success_text,
            )
            failed = harness.run_command(
                "failure",
                failure_text,
                environment_additions={
                    "LOCALCAT_TEST_SECRET": secret,
                    "LOCALCAT_TEST_ROOT": private_root,
                },
            )
            interrupted = harness.run_command(
                "timeout",
                timeout_text,
                timeout_seconds=0.1,
                windowed=True,
            )
            harness.record_event(
                operation_id="lock-observation-01",
                operation="lock",
                profile="LOCK",
                phase="acquire",
                result="OBSERVED",
                win32_code=33,
                filesystem="NTFS",
                volume_class="fixed-local",
                identity_comparison="MATCH",
                final_path_verdict="MATCH",
                reparse_verdict="CLEAR",
                share_flags=("WRITE", "READ"),
                lock_range=(0, 1),
                recovery_result="NOT_APPLICABLE",
            )
            self.register_outputs(root, harness)
            harness.finalize()

            self.assertEqual(passed["status"], "PASS")
            self.assertEqual(failed["status"], "FAIL")
            self.assertEqual(failed["exit_code"], 3)
            self.assertEqual(failed["shell_flavor"], "WINDOWS_POWERSHELL")
            self.assertEqual(failed["shell_edition"], "Desktop")
            self.assertEqual(failed["shell_process_architecture"], "X64")
            self.assertEqual(failed["shell_selection_role"], "SYSTEM32_ABSOLUTE")
            self.assertRegex(str(failed["shell_binary_sha256"]), r"[0-9a-f]{64}")
            self.assertEqual(failed["cwd_role"], "NON_REPOSITORY_CWD")
            self.assertEqual(failed["environment_profile"], "CLEAN_WINDOWS_V2")
            self.assertEqual(
                failed["environment_projection"]["LOCALCAT_TEST_SECRET"],
                "<redacted:body>",
            )
            self.assertEqual(interrupted["status"], "INTERRUPTED")
            self.assertIsNone(interrupted["exit_code"])
            manifest = self.validate(root, harness)
            self.assertEqual(manifest["run"]["status"], "INTERRUPTED")

            combined_logs = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted((root / "logs").glob("*.log"))
            )
            self.assertNotIn(secret, combined_logs)
            self.assertNotIn(private_root, combined_logs)
            self.assertIn("<redacted:body>", combined_logs)
            self.assertIn("<redacted:repository-root>", combined_logs)

    def test_silent_windowed_failure_gets_a_stable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "windowed-failure",
                        "exit 7",
                        status="FAIL",
                        exit_code=7,
                        diagnostic_code="EVIDENCE.WINDOWED.SILENT_FAILURE",
                        windowed=True,
                    )
                ],
            )
            result = harness.run_command(
                "windowed-failure",
                "exit 7",
                windowed=True,
            )
            self.register_outputs(root, harness)
            harness.finalize()

            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["exit_code"], 7)
            marker_path = root / str(result["diagnostic_marker"])
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(
                marker["stable_code"],
                "EVIDENCE.WINDOWED.SILENT_FAILURE",
            )
            self.validate(root, harness)

    def test_optional_incomplete_scenario_cannot_hide_containment_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            failing_command = "Start-Sleep -Seconds 30"
            missing_command = "Write-Output 'NOT-RUN'"
            success_command = "Write-Output 'SAFE'"
            harness = self.harness(
                root,
                scenarios=[
                    {
                        "id": "optional-incomplete",
                        "required": False,
                        "expected_observation": "optional-probe",
                        "commands": [
                            command_expectation("contained", failing_command),
                            command_expectation("missing", missing_command),
                        ],
                        "events": [],
                    },
                    {
                        "id": "mandatory-pass",
                        "required": True,
                        "expected_observation": "mandatory-probe",
                        "commands": [
                            command_expectation("success", success_command)
                        ],
                        "events": [],
                    },
                ],
            )
            with mock.patch.object(
                evidence_module._SuspendedWindowsJob,
                "assign_and_resume",
                side_effect=OSError("injected assignment failure"),
            ):
                failed = harness.run_command("contained", failing_command)
            harness.run_command("success", success_command)
            self.register_outputs(root, harness)
            harness.finalize()

            self.assertEqual(
                failed["diagnostic_code"],
                "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
            )
            manifest = self.validate(root, harness)
            self.assertEqual(manifest["run"]["status"], "FAIL")

    def test_optional_incomplete_scenario_cannot_hide_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            timeout_command = "Start-Sleep -Seconds 30"
            missing_command = "Write-Output 'NOT-RUN'"
            success_command = "Write-Output 'SAFE'"
            harness = self.harness(
                root,
                scenarios=[
                    {
                        "id": "optional-incomplete",
                        "required": False,
                        "expected_observation": "optional-probe",
                        "commands": [
                            command_expectation("timeout", timeout_command),
                            command_expectation("missing", missing_command),
                        ],
                        "events": [],
                    },
                    {
                        "id": "mandatory-pass",
                        "required": True,
                        "expected_observation": "mandatory-probe",
                        "commands": [
                            command_expectation("success", success_command)
                        ],
                        "events": [],
                    },
                ],
            )
            interrupted = harness.run_command(
                "timeout", timeout_command, timeout_seconds=0.1
            )
            harness.run_command("success", success_command)
            self.register_outputs(root, harness)
            harness.finalize()

            self.assertEqual(interrupted["status"], "INTERRUPTED")
            manifest = self.validate(root, harness)
            self.assertEqual(manifest["run"]["status"], "INTERRUPTED")

    def test_cwd_role_is_derived_and_cannot_be_spoofed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command = "Write-Output 'SAFE'"
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "repository-cwd",
                        command,
                        cwd_role="REPOSITORY_TREE",
                    )
                ],
            )
            recorded = harness.run_command(
                "repository-cwd", command, cwd=harness.repository_root
            )
            self.assertEqual(recorded["cwd_role"], "REPOSITORY_TREE")
            with self.assertRaises(TypeError):
                harness.run_command(
                    "spoofed-cwd",
                    command,
                    cwd_role="NON_REPOSITORY_CWD",
                )

    def test_clean_environment_drops_ambient_python_and_qt_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command = (
                "if ($env:PYTHONHOME -or $env:PYTHONPATH -or $env:QT_PLUGIN_PATH "
                "-or $env:QT_QPA_PLATFORM_PLUGIN_PATH) { exit 4 }; exit 0"
            )
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "clean-environment",
                        command,
                        cwd_role="REPOSITORY_TREE",
                    )
                ],
            )
            with mock.patch.dict(
                os.environ,
                {
                    "PYTHONHOME": "C:\\unsafe-python",
                    "PYTHONPATH": "C:\\unsafe-modules",
                    "QT_PLUGIN_PATH": "C:\\unsafe-qt",
                    "QT_QPA_PLATFORM_PLUGIN_PATH": "C:\\unsafe-platforms",
                },
                clear=False,
            ):
                recorded = harness.run_command(
                    "clean-environment",
                    command,
                    cwd=harness.repository_root,
                )
            self.assertEqual(recorded["status"], "PASS")
            self.assertFalse((harness.repository_root / "%SystemDrive%").exists())
            for key in (
                "PYTHONHOME",
                "PYTHONPATH",
                "QT_PLUGIN_PATH",
                "QT_QPA_PLATFORM_PLUGIN_PATH",
            ):
                self.assertNotIn(key, recorded["environment_projection"])

    def test_clean_environment_rejects_unregistered_additions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self.harness(Path(directory) / "evidence")
            with self.assertRaisesRegex(EvidenceValidationError, "LOCALCAT_"):
                harness.run_command(
                    "unsafe-environment",
                    "Write-Output 'SAFE'",
                    environment_additions={"PYTHONPATH": "C:\\unsafe"},
                )

    def test_external_contract_must_approve_every_localcat_environment_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command = "Write-Output 'SAFE'"
            harness = self.harness(
                root,
                commands=[command_expectation("unexpected-input", command)],
            )
            recorded = harness.run_command(
                "unexpected-input",
                command,
                environment_additions={"LOCALCAT_UNAPPROVED_SWITCH": "1"},
            )
            self.assertEqual(recorded["status"], "PASS")
            self.register_outputs(root, harness)
            harness.finalize()

            manifest = self.validate(root, harness)
            self.assertEqual(manifest["run"]["status"], "FAIL")
            matrix = json.loads((root / "matrix.json").read_text(encoding="utf-8"))
            self.assertEqual(matrix["scenarios"][0]["verdict"], "FAIL")

    def test_system_powershell_selection_ignores_ambient_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            fake_directory = Path(directory) / "ambient-bin"
            fake_directory.mkdir()
            fake_powershell = fake_directory / "powershell.exe"
            fake_powershell.write_bytes(b"not-an-executable")
            command = "Write-Output 'SAFE'"
            expectation = command_expectation("system-powershell", command)

            with mock.patch("shutil.which", return_value=str(fake_powershell)):
                harness = self.harness(root, commands=[expectation])
                recorded = harness.run_command("system-powershell", command)

            self.assertEqual(recorded["status"], "PASS")
            self.assertEqual(
                Path(harness.powershell_executable),
                evidence_module._system_powershell_executable(),
            )

    def test_tampered_log_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command(
                "success",
                "Write-Output 'SAFE'",
            )
            self.register_outputs(root, harness)
            harness.finalize()
            (root / "logs" / "success.stdout.log").write_text(
                "tampered\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvidenceValidationError, "artifact mismatch"):
                self.validate(root, harness)

    def test_absolute_path_in_manifest_command_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command(
                "success",
                "Write-Output 'SAFE'",
            )
            self.register_outputs(root, harness)
            manifest_path = harness.finalize()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["commands"][0]["command"] = "C:\\Users\\Private\\probe.exe"
            manifest_path.write_bytes(
                (
                    json.dumps(
                        manifest,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8"),
            )
            with self.assertRaisesRegex(EvidenceValidationError, "absolute path"):
                self.validate(root, harness)

    def test_recorded_command_must_use_portable_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(
                root,
                private_values={"source-root": str(Path(directory).resolve())},
            )
            with self.assertRaisesRegex(EvidenceValidationError, "portable placeholders"):
                harness.run_command(
                    "unsafe-command",
                    f"{Path(directory).resolve()}\\probe.exe",
                )

    def test_event_contract_rejects_illegal_share_and_lock_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self.harness(Path(directory) / "evidence")
            with self.assertRaisesRegex(EvidenceValidationError, "NONE"):
                harness.record_event(
                    operation_id="bad-share",
                    operation="lock",
                    profile="LOCK",
                    phase="acquire",
                    result="FAIL",
                    share_flags=("NONE", "READ"),
                )
            with self.assertRaisesRegex(EvidenceValidationError, "exact integer"):
                harness.record_event(
                    operation_id="bad-lock",
                    operation="lock",
                    profile="LOCK",
                    phase="acquire",
                    result="FAIL",
                    lock_range=(False, 1),
                )
            with self.assertRaisesRegex(EvidenceValidationError, "unsigned DWORD"):
                harness.record_event(
                    operation_id="bad-win32",
                    operation="lock",
                    profile="LOCK",
                    phase="acquire",
                    result="FAIL",
                    stable_code="TEST.EVENT.FAIL",
                    win32_code=-1,
                )

    def test_event_expectation_cannot_reference_another_scenario_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            event = {
                "operation_id": "cross-scenario-event",
                "command_id": "later-command",
                "operation": "probe",
                "profile": None,
                "phase": "observe",
                "result": "OBSERVED",
                "stable_code": None,
                "win32_code": None,
                "filesystem": None,
                "volume_class": None,
                "identity_comparison": "NOT_CHECKED",
                "final_path_verdict": "NOT_CHECKED",
                "reparse_verdict": "NOT_CHECKED",
                "share_flags": [],
                "lock_range": None,
                "recovery_result": "NOT_APPLICABLE",
            }
            with self.assertRaisesRegex(
                EvidenceValidationError,
                "not a recorded command",
            ):
                self.harness(
                    Path(directory) / "evidence",
                    scenarios=[
                        {
                            "id": "event-owner",
                            "required": False,
                            "expected_observation": "event-probe",
                            "commands": [],
                            "events": [event],
                        },
                        {
                            "id": "command-owner",
                            "required": True,
                            "expected_observation": "command-probe",
                            "commands": [
                                command_expectation(
                                    "later-command", "Write-Output 'SAFE'"
                                )
                            ],
                            "events": [],
                        },
                    ],
                )

    def test_finalize_requires_all_release_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command(
                "success",
                "Write-Output 'SAFE'",
            )
            with self.assertRaisesRegex(EvidenceValidationError, "both adaptation checklists"):
                harness.finalize()

    def test_empty_run_cannot_be_finalized_as_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            self.register_outputs(root, harness)
            with self.assertRaisesRegex(EvidenceValidationError, "at least one command"):
                harness.finalize()

    def test_external_contract_can_approve_an_exact_expected_negative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "expected-rejection",
                        "exit 9",
                        status="FAIL",
                        exit_code=9,
                    )
                ],
            )
            command = harness.run_command("expected-rejection", "exit 9")
            self.assertEqual(command["status"], "FAIL")
            self.register_outputs(root, harness)
            manifest_path = harness.finalize()
            manifest = self.validate(root, harness)
            self.assertEqual(manifest["run"]["status"], "PASS")

            digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            self.validate(root, harness, expected_manifest_sha256=digest)
            with self.assertRaisesRegex(EvidenceValidationError, "external digest anchor"):
                self.validate(root, harness, expected_manifest_sha256="0" * 64)

    def test_validator_binds_contract_key_to_the_trusted_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command("success", "Write-Output 'SAFE'")
            self.register_outputs(root, harness)
            harness.finalize()

            with self.assertRaisesRegex(
                EvidenceValidationError,
                "external commit anchor",
            ):
                validate_evidence_bundle(
                    root,
                    repository_root=harness.repository_root,
                    scenario_contract_key=harness.scenario_contract_key,
                    expected_repository_commit="0" * 40,
                    expected_scenario_contract_sha256=(
                        harness.scenario_contract_sha256
                    ),
                )
            with self.assertRaisesRegex(
                EvidenceValidationError,
                "external digest anchor",
            ):
                validate_evidence_bundle(
                    root,
                    repository_root=harness.repository_root,
                    scenario_contract_key=harness.scenario_contract_key,
                    expected_repository_commit=harness.repository_commit,
                    expected_scenario_contract_sha256="0" * 64,
                )

            alias_key = (
                "packaging/windows/evidence-scenarios/"
                "substitute-scenario-contract.json"
            )
            alias_path = harness.repository_root.joinpath(*alias_key.split("/"))
            shutil.copyfile(harness.scenario_contract_path, alias_path)
            with self.assertRaisesRegex(
                EvidenceValidationError,
                "does not match the trusted repository key",
            ):
                validate_evidence_bundle(
                    root,
                    repository_root=harness.repository_root,
                    scenario_contract_key=alias_key,
                    expected_repository_commit=harness.repository_commit,
                    expected_scenario_contract_sha256=(
                        harness.scenario_contract_sha256
                    ),
                )

            with self.assertRaisesRegex(
                EvidenceValidationError,
                "regular repository file",
            ):
                validate_evidence_bundle(
                    root,
                    repository_root=harness.repository_root,
                    scenario_contract_key=(
                        "packaging/windows/evidence-scenarios/"
                        "missing-scenario-contract.json"
                    ),
                    expected_repository_commit=harness.repository_commit,
                    expected_scenario_contract_sha256=(
                        harness.scenario_contract_sha256
                    ),
                )

            with self.assertRaisesRegex(
                EvidenceValidationError,
                "packaging/windows/evidence-scenarios",
            ):
                validate_evidence_bundle(
                    root,
                    repository_root=harness.repository_root,
                    scenario_contract_key="unapproved-contract.json",
                    expected_repository_commit=harness.repository_commit,
                    expected_scenario_contract_sha256=(
                        harness.scenario_contract_sha256
                    ),
                )

    def test_matrix_cannot_override_external_contract_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "expected-rejection",
                        "exit 9",
                        status="FAIL",
                        exit_code=8,
                    )
                ],
            )
            harness.run_command("expected-rejection", "exit 9")
            self.register_outputs(root, harness)
            matrix_path = root / "matrix.json"
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            self.assertEqual(matrix["scenarios"][0]["verdict"], "FAIL")
            matrix["scenarios"][0]["verdict"] = "PASS"
            matrix["scenarios"][0]["stable_code"] = None
            matrix_path.write_bytes(
                (
                    json.dumps(
                        matrix,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
            )
            with self.assertRaisesRegex(EvidenceValidationError, "scenario facts"):
                harness.finalize()

    def test_timeout_terminates_the_power_shell_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command_text = (
                "$child = Start-Process powershell.exe "
                "-ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 30' "
                "-PassThru; Write-Output $child.Id; Start-Sleep -Seconds 30"
            )
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "process-tree-timeout",
                        command_text,
                        status="INTERRUPTED",
                        exit_code=None,
                        diagnostic_code="EVIDENCE.COMMAND.TIMEOUT",
                        windowed=True,
                    )
                ],
            )
            result = harness.run_command(
                "process-tree-timeout",
                command_text,
                timeout_seconds=1.5,
                windowed=True,
            )
            self.assertEqual(result["status"], "INTERRUPTED")
            stdout_path = root / str(result["stdout_log"])
            child_ids = [
                int(line.strip())
                for line in stdout_path.read_text(encoding="utf-8").splitlines()
                if line.strip().isdigit()
            ]
            self.assertEqual(len(child_ids), 1)

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel32.WaitForSingleObject.restype = ctypes.c_ulong
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            child_handle = kernel32.OpenProcess(0x00100000, False, child_ids[0])
            if child_handle:
                try:
                    self.assertEqual(kernel32.WaitForSingleObject(child_handle, 0), 0)
                finally:
                    kernel32.CloseHandle(child_handle)
            self.register_outputs(root, harness)
            harness.finalize()
            self.validate(root, harness)

    def test_assignment_failure_kills_the_unassigned_suspended_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command_text = "Start-Sleep -Seconds 30"
            harness = self.harness(
                root,
                commands=[command_expectation("assignment-failure", command_text)],
            )
            started = time.monotonic()
            with mock.patch.object(
                evidence_module._SuspendedWindowsJob,
                "assign_and_resume",
                side_effect=OSError("injected assignment failure"),
            ):
                result = harness.run_command("assignment-failure", command_text)
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(
                result["diagnostic_code"],
                "EVIDENCE.COMMAND.CONTAINMENT_UNAVAILABLE",
            )

    def test_terminate_failure_falls_back_to_job_close_and_bounded_drain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            command_text = "Start-Sleep -Seconds 30"
            harness = self.harness(
                root,
                commands=[
                    command_expectation(
                        "terminate-failure",
                        command_text,
                        status="INTERRUPTED",
                        exit_code=None,
                        diagnostic_code="EVIDENCE.COMMAND.TIMEOUT",
                    )
                ],
            )
            started = time.monotonic()
            with mock.patch.object(
                evidence_module._SuspendedWindowsJob,
                "terminate",
                side_effect=OSError("injected terminate failure"),
            ):
                result = harness.run_command(
                    "terminate-failure", command_text, timeout_seconds=0.1
                )
            self.assertLess(time.monotonic() - started, 8)
            self.assertEqual(result["status"], "INTERRUPTED")

    def test_hidden_and_unsupported_artifacts_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command("success", "Write-Output 'SAFE'")
            self.register_outputs(root, harness)
            (root / "payload.bin").write_bytes(b"not-an-evidence-format")
            with self.assertRaisesRegex(EvidenceValidationError, "unsupported evidence artifact"):
                harness.finalize()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command("success", "Write-Output 'SAFE'")
            self.register_outputs(root, harness)
            harness.finalize()
            (root / ".secret").write_text("secret", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "hidden or temporary"):
                self.validate(root, harness)

    def test_private_path_in_event_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self.harness(Path(directory) / "evidence")
            with self.assertRaisesRegex(EvidenceValidationError, "private absolute path"):
                harness.record_event(
                    operation_id="unsafe-event",
                    operation="C:/Users/Private/source",
                    profile=None,
                    phase="read",
                    result="FAIL",
                )

    def test_registered_secret_cannot_enter_event_or_release_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            secret = "body-secret-value"
            harness = self.harness(root, private_values={"body": secret})
            with self.assertRaisesRegex(EvidenceValidationError, "registered private data"):
                harness.record_event(
                    operation_id="secret-event",
                    operation="read",
                    profile=None,
                    phase=secret,
                    result="FAIL",
                    stable_code="TEST.EVENT.FAIL",
                )

            (root / "matrix.json").write_text(
                json.dumps({"unsafe": secret}),
                encoding="utf-8",
            )
            (root / "windows-fs-lock-adaptation-checklist.md").write_text(
                "# Safe\n",
                encoding="utf-8",
            )
            (root / "frozen-source-packaging-checklist.md").write_text(
                "# Safe\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvidenceValidationError, "registered private data"):
                harness.register_release_outputs(
                    matrix="matrix.json",
                    windows_fs_lock_checklist="windows-fs-lock-adaptation-checklist.md",
                    frozen_source_packaging_checklist="frozen-source-packaging-checklist.md",
                )

    def test_registered_secret_cannot_enter_manifest_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            secret = "body-secret-value"
            with self.assertRaisesRegex(EvidenceValidationError, "registered private data"):
                self.harness(
                    root,
                    private_values={"body": secret},
                    repository_branch=secret,
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            secret = "body-secret-value"
            unsafe_environment = environment()
            unsafe_environment["os_version"] = secret
            with self.assertRaisesRegex(EvidenceValidationError, "registered private data"):
                self.harness(
                    root,
                    private_values={"body": secret},
                    environment_override=unsafe_environment,
                )

    def test_release_output_roles_must_be_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "evidence"
            harness = self.harness(root)
            harness.run_command("success", "Write-Output 'SAFE'")
            harness.write_release_matrix()
            checklist = root / "checklist.md"
            checklist.write_text("# Checklist\n", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceValidationError, "distinct files"):
                harness.register_release_outputs(
                    matrix="matrix.json",
                    windows_fs_lock_checklist="checklist.md",
                    frozen_source_packaging_checklist="checklist.md",
                )


if __name__ == "__main__":
    unittest.main()
