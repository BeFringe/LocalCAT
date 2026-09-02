"""Windows public fresh-process activation/recovery acceptance."""

from __future__ import annotations

from collections.abc import Callable
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_WORKER = "tests.windows_tm_portable_publication_recovery_worker"
_PROCESS_SCHEMA = "localcat.windows-tm-activation-process-recovery.v1"
_REBOOT_TICKET_SCHEMA = "localcat.windows-tm-activation-reboot-ticket.v1"
_PHASE_PREFIXES = {
    "PREPARED": [],
    "DB_REPLACED": ["DB_REPLACED"],
    "MANIFEST_PUBLISHED": ["DB_REPLACED", "MANIFEST_PUBLISHED"],
    "GENERATION_PUBLISHED": [
        "DB_REPLACED",
        "MANIFEST_PUBLISHED",
        "GENERATION_PUBLISHED",
    ],
}


def _run_worker(
    root: Path,
    mode: str,
    *,
    phase: str | None = None,
    mutation: str | None = None,
    extra: tuple[str, ...] = (),
) -> dict:
    command = [sys.executable, "-B", "-m", _WORKER, mode, str(root)]
    if phase is not None:
        command.extend(("--phase", phase))
    if mutation is not None:
        command.extend(("--mutation", mutation))
    command.extend(extra)
    completed = subprocess.run(
        command,
        cwd=_SOURCE_ROOT,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"worker {mode}/{phase} exited {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise AssertionError(
            f"worker {mode}/{phase} emitted {len(lines)} lines\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(lines[0])


def _worker_command(root: Path, mode: str, *extra: str) -> list[str]:
    return [sys.executable, "-B", "-m", _WORKER, mode, str(root), *extra]


def _wait_for_marker(
    marker: Path,
    process: subprocess.Popen[bytes],
    *,
    timeout: float = 180.0,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while not marker.is_file() or marker.stat().st_size == 0:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "worker exited before marker: "
                f"{process.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
            )
        if time.monotonic() >= deadline:
            raise AssertionError("worker marker timeout")
        time.sleep(0.02)
    return json.loads(marker.read_text(encoding="utf-8"))


def _terminate(process: subprocess.Popen[bytes], exit_code: int = 93) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate = kernel32.TerminateProcess
    terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate.restype = ctypes.c_int32
    if not terminate(int(process._handle), exit_code):
        raise ctypes.WinError(ctypes.get_last_error())


def _kill_at(
    root: Path,
    phase: str,
    marker: Path,
    *,
    before_terminate: Callable[[dict[str, object]], None] | None = None,
) -> dict[str, object]:
    process = subprocess.Popen(
        _worker_command(
            root,
            "activation-fault",
            "--phase",
            phase,
            "--marker",
            str(marker),
        ),
        cwd=_SOURCE_ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    terminated = False
    try:
        observed = _wait_for_marker(marker, process)
        if observed["schema"] != _PROCESS_SCHEMA or observed["phase"] != phase:
            raise AssertionError(f"unexpected marker: {observed!r}")
        if before_terminate is not None:
            before_terminate(observed)
        _terminate(process)
        terminated = True
        stdout, stderr = process.communicate(timeout=30.0)
        if process.returncode != 93:
            raise AssertionError(
                f"terminated worker exit mismatch: {process.returncode}; "
                f"stdout={stdout!r}; stderr={stderr!r}"
            )
        return observed
    finally:
        if process.poll() is None:
            _terminate(process)
            process.communicate(timeout=30.0)
        if not terminated and marker.exists():
            marker.unlink()


def _remove_long_quarantine(root: Path) -> None:
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


def _assert_no_worker_exception(
    case: unittest.TestCase,
    result: dict,
) -> None:
    case.assertNotIn("exception_type", result, result.get("traceback", result))


def _assert_complete(
    case: unittest.TestCase,
    result: dict,
) -> None:
    _assert_no_worker_exception(case, result)
    case.assertEqual(result["outcome"]["kind"], "MigrationReport", result)
    case.assertEqual(result["outcome"]["generation"], 0)
    case.assertEqual(result["outcome"]["record_count"], 3)
    runtime = result["runtime"]
    case.assertEqual(runtime["state"], "READY")
    case.assertEqual(runtime["generation"], 0)
    case.assertTrue(runtime["view_visible"])
    case.assertIsNotNone(runtime["database"])
    case.assertIsNotNone(runtime["manifest"])
    case.assertTrue(runtime["lineage_marker"]["exact"])
    canonical = runtime["canonical"]
    case.assertTrue(canonical["fts5_runtime"])
    case.assertEqual(canonical["fts_count"], 3)
    case.assertEqual(canonical["fts_matches"], 2)
    case.assertEqual(canonical["same_targets"], ["winner", "first"])
    case.assertEqual(canonical["meta"]["activation_status"], "ACTIVE")
    case.assertEqual(canonical["meta"]["generation"], "0")
    case.assertEqual(canonical["meta"]["fts5_available"], "1")
    case.assertEqual(canonical["health"]["generation"], 0)
    case.assertTrue(canonical["health"]["healthy"])
    case.assertTrue(canonical["health"]["exact_available"])
    case.assertEqual(canonical["health"]["index_kind"], "FTS5_TRIGRAM")
    journal = result["journal"]
    case.assertEqual(
        journal["phases"],
        ["DB_REPLACED", "MANIFEST_PUBLISHED", "GENERATION_PUBLISHED"],
    )
    case.assertTrue(journal["predecessor_closed"])
    case.assertEqual(journal["candidate_residue"], [])
    case.assertEqual(journal["initial_stage_residue"], [])
    retirement = journal["retirement"]
    case.assertTrue(retirement["source_absent"])
    case.assertTrue(
        all(len(matches) == 1 for matches in retirement["quarantine_matches"].values()),
        retirement,
    )


def _assert_unavailable(
    case: unittest.TestCase,
    result: dict,
) -> None:
    _assert_no_worker_exception(case, result)
    case.assertEqual(result["outcome"]["kind"], "MigrationFailure", result)
    case.assertEqual(
        result["outcome"]["error_code"],
        "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
    )
    case.assertFalse(result["outcome"]["retryable"])
    case.assertTrue(result["outcome"]["canonical_authority_ambiguous"])
    case.assertIsNone(result["runtime"]["generation"])
    case.assertFalse(result["runtime"]["view_visible"])


def _assert_complete_or_recovery_only(
    case: unittest.TestCase,
    result: dict,
) -> str:
    _assert_no_worker_exception(case, result)
    kind = result["outcome"]["kind"]
    if kind == "MigrationReport":
        _assert_complete(case, result)
        return "COMPLETE_GEN0"
    case.assertEqual(kind, "MigrationFailure", result)
    case.assertFalse(result["runtime"]["view_visible"], result)
    case.assertIsNone(result["runtime"]["generation"], result)
    case.assertIn(
        result["outcome"]["error_code"],
        {
            "MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE",
            "MIGRATION.INITIAL_RECOVERED_CANCELLED",
        },
        result,
    )
    return "RECOVERY_ONLY"


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsPortableFreshProcessRecoveryTests(unittest.TestCase):
    def test_prepared_cancels_then_fresh_retry_and_replay_complete_once(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="PREPARED")
            recover = _run_worker(root, "recover")
            retry = _run_worker(root, "retry")
            replay = _run_worker(root, "replay")

            for result in (creator, recover, retry, replay):
                _assert_no_worker_exception(self, result)
            self.assertEqual(
                [item["mode"] for item in (creator, recover, retry, replay)],
                ["create", "recover", "retry", "replay"],
            )
            self.assertEqual(creator["boundary_calls"], 1)
            self.assertEqual(creator["issue_count"], 1)
            self.assertEqual(creator["journal"]["phases"], [])
            self.assertEqual(creator["runtime"]["state"], "ACTIVATING")
            self.assertIsNone(creator["runtime"]["generation"])
            self.assertFalse(creator["runtime"]["view_visible"])

            self.assertEqual(recover["outcome"]["kind"], "MigrationFailure", recover)
            self.assertEqual(
                recover["outcome"]["error_code"],
                "MIGRATION.INITIAL_RECOVERED_CANCELLED",
            )
            self.assertTrue(recover["outcome"]["retryable"])
            self.assertEqual(recover["runtime"]["state"], "READY")
            self.assertIsNone(recover["runtime"]["generation"])
            self.assertFalse(recover["runtime"]["view_visible"])
            self.assertIsNone(recover["runtime"]["database"])
            self.assertIsNone(recover["runtime"]["manifest"])

            _assert_complete(self, retry)
            _assert_complete(self, replay)
            self.assertEqual(
                retry["outcome"]["contract_json"],
                replay["outcome"]["contract_json"],
            )
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()


    def test_real_fts5_publish_and_two_cold_queries_close_identically(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "fts5-activate")
            first = _run_worker(root, "fts5-cold-query")
            second = _run_worker(root, "fts5-cold-query")

            for result in (creator, first, second):
                _assert_no_worker_exception(self, result)
            self.assertEqual(creator["outcome"]["kind"], "MigrationReport")
            self.assertEqual(creator["outcome"]["generation"], 0)
            self.assertEqual(creator["outcome"]["record_count"], 3)
            self.assertTrue(creator["runtime"]["canonical"]["fts5_runtime"])

            for result in (first, second):
                capability = result["runtime_capability"]
                self.assertTrue(capability["fts5_available"])
                self.assertRegex(capability["sqlite_version"], r"^\d+\.\d+")
                self.assertRegex(capability["unicode_version"], r"^\d+\.\d+")

                health = result["health"]
                self.assertTrue(health["healthy"])
                self.assertTrue(health["leased_equal"])
                self.assertTrue(health["exact_available"])
                self.assertEqual(health["generation"], 0)
                self.assertEqual(health["record_count"], 3)
                self.assertEqual(health["index_kind"], "FTS5_TRIGRAM")
                self.assertEqual(health["source_binding_state"], "VERIFIED_CURRENT")
                self.assertEqual(health["diagnostic_codes"], [])
                self.assertRegex(health["snapshot_binding_digest"], r"^[0-9a-f]{64}$")

                query = result["query"]
                self.assertEqual(query["folded_query"], "sam")
                self.assertEqual(query["candidate_ids"], [1, 2])
                self.assertEqual(query["record_ids"], [1, 2])
                self.assertEqual(query["record_sources"], ["same", "same"])
                self.assertEqual(query["record_targets"], ["first", "winner"])
                self.assertEqual(query["candidate_pretruncate_ranks"], [1, 2])
                self.assertEqual(query["index_kind"], "FTS5_TRIGRAM")
                self.assertTrue(query["fuzzy_available"])
                self.assertIsNone(query["unavailable_code"])
                self.assertEqual(query["candidate_stages"][0], "FTS_TRIGRAM")
                self.assertTrue(
                    all(
                        "FTS_TRIGRAM" in stages
                        for stages in query["candidate_recall_stages"]
                    )
                )

                database = result["disk"]["database"]
                manifest = result["disk"]["manifest"]
                source = result["disk"]["source"]
                for authority in (database, manifest, source):
                    self.assertRegex(authority["sha256"], r"^[0-9a-f]{64}$")
                sql = result["disk"]["database_sql"]
                self.assertEqual(sql["integrity_check"], "ok")
                self.assertEqual(sql["foreign_key_check"], [])
                self.assertIn("ENABLE_FTS5", sql["compile_options"])
                self.assertIn("tm_fts", sql["schema_names"])
                self.assertIn("CREATE VIRTUAL TABLE tm_fts USING fts5", sql["fts_ddl"])
                self.assertIn("tokenize='trigram case_sensitive 1'", sql["fts_ddl"])
                self.assertEqual(sql["meta"]["candidate_index_kind"], "FTS5_TRIGRAM")
                self.assertEqual(sql["meta"]["fts5_available"], "1")
                self.assertRegex(sql["meta"]["schema_digest"], r"^[0-9a-f]{64}$")
                self.assertRegex(sql["meta"]["activation_digest"], r"^[0-9a-f]{64}$")
                attestation = result["journal"]["active_attestation"]
                self.assertEqual(attestation["generation"], 0)
                self.assertEqual(attestation["index_kind"], "FTS5_TRIGRAM")
                self.assertTrue(attestation["fts5_available"])
                self.assertEqual(
                    attestation["sqlite_version"],
                    capability["sqlite_version"],
                )
                self.assertEqual(
                    attestation["unicode_version"],
                    capability["unicode_version"],
                )
                self.assertEqual(attestation["database_sha256"], database["sha256"])
                self.assertEqual(attestation["manifest_sha256"], manifest["sha256"])
                self.assertEqual(attestation["source_sha256"], source["sha256"])
                self.assertRegex(attestation["attestation_digest"], r"^[0-9a-f]{64}$")

            self.assertEqual(first["runtime_capability"], second["runtime_capability"])
            self.assertEqual(first["health"], second["health"])
            self.assertEqual(first["query"], second["query"])
            self.assertEqual(first["journal"], second["journal"])
            self.assertEqual(first["disk"], second["disk"])
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_lock_owner_kill_releases_kernel_lease_to_fresh_public_retry(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(temporary.name).resolve() / "lock-owner-kill"
        marker = Path(temporary.name).resolve() / "lock-owner.ready.json"
        owner = subprocess.Popen(
            _worker_command(
                root,
                "activation-fault",
                "--phase",
                "LOCK_HELD",
                "--marker",
                str(marker),
            ),
            cwd=_SOURCE_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        terminated = False
        try:
            observed = _wait_for_marker(marker, owner)
            self.assertEqual(observed["phase"], "LOCK_HELD")
            self.assertEqual(observed["schema"], _PROCESS_SCHEMA)

            cold_open = _run_worker(root, "cold-open-probe")
            self.assertEqual(
                cold_open["runtime_profile"],
                "LEGACY",
                cold_open,
            )
            self.assertFalse(cold_open["canonical_active"], cold_open)
            self.assertFalse(cold_open["canonical_store_visible"], cold_open)
            self.assertEqual(
                cold_open["exact_result"],
                {"source": "same", "target": "winner"},
                cold_open,
            )
            self.assertNotEqual(observed["pid"], cold_open["pid"])

            contention_started = time.monotonic()
            contender = _run_worker(root, "activate")
            contention_seconds = time.monotonic() - contention_started
            _assert_unavailable(self, contender)
            self.assertLess(contention_seconds, 30.0)
            self.assertNotEqual(observed["pid"], contender["pid"])
            self.assertFalse(contender["runtime"]["view_visible"])
            self.assertEqual(contender["journal"]["phases"], [])

            _terminate(owner)
            terminated = True
            stdout, stderr = owner.communicate(timeout=30.0)
            self.assertEqual(
                owner.returncode,
                93,
                msg=f"stdout={stdout!r}; stderr={stderr!r}",
            )

            retry = _run_worker(root, "retry")
            replay = _run_worker(root, "replay")
            _assert_complete(self, retry)
            _assert_complete(self, replay)
            self.assertEqual(
                retry["outcome"]["contract_json"],
                replay["outcome"]["contract_json"],
            )
            self.assertNotEqual(retry["pid"], contender["pid"])
        finally:
            if owner.poll() is None:
                _terminate(owner)
                owner.communicate(timeout=30.0)
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_terminate_process_at_each_durable_phase_recovers_only_once(self) -> None:
        for phase in (
            "PREPARED",
            "DB_REPLACED",
            "MANIFEST_PUBLISHED",
            "GENERATION_PUBLISHED",
        ):
            with self.subTest(phase=phase):
                temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
                base = Path(temporary.name).resolve()
                root = base / f"phase-{phase.lower()}"
                marker = base / f"{phase}.ready.json"
                try:
                    _kill_at(root, phase, marker)
                    killed = _run_worker(root, "inspect")
                    _assert_no_worker_exception(self, killed)
                    self.assertEqual(
                        killed["journal"]["phases"],
                        _PHASE_PREFIXES[phase],
                    )
                    self.assertIsNotNone(killed["journal"]["prepared"])

                    recovered = _run_worker(root, "recover")
                    state = _assert_complete_or_recovery_only(self, recovered)
                    if phase == "PREPARED":
                        self.assertEqual(state, "RECOVERY_ONLY")
                        self.assertEqual(
                            recovered["outcome"]["error_code"],
                            "MIGRATION.INITIAL_RECOVERED_CANCELLED",
                        )
                        completed = _run_worker(root, "retry")
                        _assert_complete(self, completed)
                    else:
                        self.assertEqual(state, "COMPLETE_GEN0")
                        completed = recovered
                    if phase in {"DB_REPLACED", "MANIFEST_PUBLISHED"}:
                        self.assertEqual(
                            recovered["full_validation_calls"],
                            1,
                            recovered,
                        )
                    replay = _run_worker(root, "replay")
                    _assert_complete(self, replay)
                    self.assertEqual(
                        completed["outcome"]["contract_json"],
                        replay["outcome"]["contract_json"],
                    )
                finally:
                    if root.exists():
                        _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_db_and_manifest_pending_boundaries_never_expose_partial_view(self) -> None:
        boundaries = (
            "post_begin",
            "owner_durable",
            "business_reproof",
            "terminal_reproof",
        )
        for asset in ("DB", "MANIFEST"):
            for boundary in boundaries:
                phase = f"{asset}.{boundary}"
                with self.subTest(phase=phase):
                    temporary = tempfile.TemporaryDirectory(
                        ignore_cleanup_errors=True
                    )
                    base = Path(temporary.name).resolve()
                    root = base / f"pending-{asset.lower()}-{boundary}"
                    marker = base / f"{asset}.{boundary}.ready.json"
                    try:
                        def assert_parked(observed: dict[str, object]) -> None:
                            details = observed["details"]
                            self.assertIs(type(details), dict)
                            self.assertEqual(details["asset"], asset)
                            self.assertEqual(details["boundary"], boundary)
                            self.assertEqual(
                                details["coordinator"],
                                {
                                    "generation": None,
                                    "state": "ACTIVATING",
                                    "view_visible": False,
                                },
                            )
                            self.assertNotIn("outcome", observed)

                        _kill_at(
                            root,
                            phase,
                            marker,
                            before_terminate=assert_parked,
                        )
                        killed = _run_worker(root, "inspect")
                        _assert_no_worker_exception(self, killed)
                        self.assertIsNone(
                            killed["inventory"]["lineage_marker"],
                            killed,
                        )
                        expected_prefix = (
                            []
                            if asset == "DB" and boundary == "post_begin"
                            else ["DB_REPLACED"]
                            if asset == "MANIFEST" and boundary == "post_begin"
                            else ["DB_REPLACED"]
                            if asset == "DB"
                            else ["DB_REPLACED", "MANIFEST_PUBLISHED"]
                        )
                        self.assertEqual(
                            killed["journal"]["phases"],
                            expected_prefix,
                            killed,
                        )
                        cold_open = _run_worker(root, "cold-open-probe")
                        self.assertEqual(
                            cold_open["runtime_profile"],
                            "UNAVAILABLE",
                            cold_open,
                        )
                        self.assertFalse(cold_open["canonical_active"], cold_open)
                        self.assertFalse(
                            cold_open["canonical_store_visible"],
                            cold_open,
                        )
                        self.assertIsNone(cold_open["exact_result"], cold_open)

                        recovered = _run_worker(root, "recover")
                        result_state = _assert_complete_or_recovery_only(
                            self,
                            recovered,
                        )
                        if result_state == "COMPLETE_GEN0":
                            self.assertIsNotNone(recovered["runtime"]["database"])
                            self.assertIsNotNone(recovered["runtime"]["manifest"])
                            replay = _run_worker(root, "replay")
                            _assert_complete(self, replay)
                            self.assertEqual(
                                recovered["outcome"]["contract_json"],
                                replay["outcome"]["contract_json"],
                            )
                        else:
                            self.assertFalse(recovered["runtime"]["view_visible"])
                            self.assertIsNone(recovered["runtime"]["generation"])
                    finally:
                        if root.exists():
                            _remove_long_quarantine(root)
                        temporary.cleanup()

    def test_activation_reboot_ticket_same_session_is_canonical_not_run(self) -> None:
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        base = Path(temporary.name).resolve()
        root = base / "activation-reboot-root"
        ticket = base / "activation-reboot-ticket.json"
        try:
            prepared = _run_worker(
                root,
                "activation-reboot-prepare",
                extra=("--ticket", str(ticket)),
            )
            self.assertEqual(prepared["reboot"], "PREPARED")
            self.assertEqual(prepared["schema"], _REBOOT_TICKET_SCHEMA)
            raw = ticket.read_bytes()
            decoded = json.loads(raw.decode("utf-8"))
            self.assertEqual(decoded["schema"], _REBOOT_TICKET_SCHEMA)
            self.assertEqual(decoded["root"], str(root))
            self.assertEqual(decoded["expected"]["generation"], 0)
            self.assertEqual(decoded["expected"]["record_count"], 3)
            self.assertEqual(
                decoded["inventory"]["journal"]["phases"],
                ["DB_REPLACED", "MANIFEST_PUBLISHED", "GENERATION_PUBLISHED"],
            )
            unsigned = dict(decoded)
            digest = unsigned.pop("ticket_sha256")
            canonical_unsigned = (
                json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            self.assertEqual(digest, hashlib.sha256(canonical_unsigned).hexdigest())
            before = _run_worker(root, "inspect")

            same_session = _run_worker(
                root,
                "activation-reboot-resume",
                extra=("--ticket", str(ticket)),
            )
            self.assertEqual(same_session["reboot"], "NOT_RUN")
            self.assertEqual(same_session["ticket_sha256"], digest)
            after = _run_worker(root, "inspect")
            self.assertEqual(before["inventory"], after["inventory"])
            self.assertEqual(before["journal"], after["journal"])
        finally:
            if root.exists():
                _remove_long_quarantine(root)
            temporary.cleanup()

    def test_each_published_prefix_recovers_and_replays_generation_zero(self) -> None:
        for phase in ("DB_REPLACED", "MANIFEST_PUBLISHED", "GENERATION_PUBLISHED"):
            with self.subTest(phase=phase):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    creator = _run_worker(root, "create", phase=phase)
                    recover = _run_worker(root, "recover")
                    replay = _run_worker(root, "replay")

                    for result in (creator, recover, replay):
                        _assert_no_worker_exception(self, result)
                    self.assertEqual(
                        [item["mode"] for item in (creator, recover, replay)],
                        ["create", "recover", "replay"],
                    )
                    self.assertEqual(creator["boundary_calls"], 1)
                    self.assertEqual(creator["issue_count"], 1)
                    self.assertEqual(
                        creator["journal"]["phases"],
                        _PHASE_PREFIXES[phase],
                    )
                    self.assertEqual(creator["runtime"]["state"], "ACTIVATING")
                    self.assertIsNone(creator["runtime"]["generation"])
                    self.assertFalse(creator["runtime"]["view_visible"])

                    _assert_complete(self, recover)
                    _assert_complete(self, replay)
                    self.assertEqual(
                        recover["outcome"]["contract_json"],
                        replay["outcome"]["contract_json"],
                    )
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_active_database_before_manifest_copy_recovers_without_rebuild(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="DB_ACTIVE")
            recover = _run_worker(root, "recover")
            replay = _run_worker(root, "replay")

            for result in (creator, recover, replay):
                _assert_no_worker_exception(self, result)
            self.assertEqual(creator["boundary_calls"], 1)
            self.assertEqual(creator["issue_count"], 1)
            self.assertEqual(creator["outcome"]["kind"], "MigrationFailure")
            self.assertEqual(creator["journal"]["phases"], ["DB_REPLACED"])
            self.assertEqual(creator["runtime"]["state"], "ACTIVATING")
            self.assertIsNone(creator["runtime"]["generation"])
            self.assertFalse(creator["runtime"]["view_visible"])
            self.assertIsNone(creator["runtime"]["manifest"])
            self.assertEqual(
                creator["runtime"]["database_sql"]["meta"][
                    "activation_status"
                ],
                "ACTIVE",
            )
            self.assertEqual(
                creator["runtime"]["database_sql"]["receipt_statuses"],
                ["completed"],
            )

            _assert_complete(self, recover)
            _assert_complete(self, replay)
            self.assertEqual(
                recover["outcome"]["contract_json"],
                replay["outcome"]["contract_json"],
            )
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_manifest_phase_without_canonical_manifest_fails_unchanged(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="MANIFEST_PUBLISHED")
            mutation = _run_worker(
                root,
                "mutate",
                mutation="missing-manifest",
            )
            recover = _run_worker(root, "recover")

            for result in (creator, mutation, recover):
                _assert_no_worker_exception(self, result)
            _assert_unavailable(self, recover)
            self.assertIsNone(mutation["disk"]["manifest"])
            self.assertIsNone(recover["runtime"]["manifest"])
            self.assertEqual(
                mutation["disk"]["database"],
                recover["runtime"]["database"],
            )
            self.assertEqual(
                mutation["journal"]["private_namespace"],
                recover["journal"]["private_namespace"],
            )
            self.assertEqual(
                mutation["journal"]["phases"],
                ["DB_REPLACED", "MANIFEST_PUBLISHED"],
            )
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()

    def test_active_phase_rejects_exact_sealed_database_rollback(self) -> None:
        for phase in ("MANIFEST_PUBLISHED", "GENERATION_PUBLISHED"):
            with self.subTest(phase=phase):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    creator = _run_worker(root, "create", phase=phase)
                    mutation = _run_worker(
                        root,
                        "mutate",
                        mutation="sealed-database",
                    )
                    recover = _run_worker(root, "recover")

                    for result in (creator, mutation, recover):
                        _assert_no_worker_exception(self, result)
                    _assert_unavailable(self, recover)
                    self.assertEqual(
                        mutation["disk"]["database_sql"]["meta"][
                            "activation_status"
                        ],
                        "SEALED",
                    )
                    self.assertEqual(
                        mutation["disk"]["database"],
                        recover["runtime"]["database"],
                    )
                    self.assertEqual(
                        mutation["journal"]["private_namespace"],
                        recover["journal"]["private_namespace"],
                    )
                    self.assertEqual(
                        recover["journal"]["phases"],
                        _PHASE_PREFIXES[phase],
                    )
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_db_phase_rejects_incomplete_or_changed_owner_facts(self) -> None:
        for mutation_name in (
            "stage-database-missing",
            "stage-database-changed",
            "source-changed",
            "wrong-manifest",
        ):
            with self.subTest(mutation=mutation_name):
                temporary = tempfile.TemporaryDirectory()
                root = Path(temporary.name).resolve()
                try:
                    creator = _run_worker(root, "create", phase="DB_REPLACED")
                    mutation = _run_worker(
                        root,
                        "mutate",
                        mutation=mutation_name,
                    )
                    recover = _run_worker(root, "recover")

                    for result in (creator, mutation, recover):
                        _assert_no_worker_exception(self, result)
                    _assert_unavailable(self, recover)
                    self.assertEqual(
                        mutation["disk"]["database_sql"]["meta"][
                            "activation_status"
                        ],
                        "SEALED",
                    )
                    self.assertEqual(
                        mutation["disk"]["database_sql"]["receipt_statuses"],
                        ["issued"],
                    )
                    self.assertEqual(
                        mutation["disk"]["database"],
                        recover["runtime"]["database"],
                    )
                    self.assertEqual(
                        mutation["journal"]["private_namespace"],
                        recover["journal"]["private_namespace"],
                    )
                    self.assertEqual(
                        mutation["journal"]["stage_assets"],
                        recover["journal"]["stage_assets"],
                    )
                    self.assertEqual(recover["journal"]["phases"], ["DB_REPLACED"])
                finally:
                    _remove_long_quarantine(root)
                    temporary.cleanup()

    def test_unknown_private_entry_latches_actual_coordinator_failed(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name).resolve()
        try:
            creator = _run_worker(root, "create", phase="MANIFEST_PUBLISHED")
            mutation = _run_worker(
                root,
                "mutate",
                mutation="unknown-private",
            )
            recover = _run_worker(root, "recover")

            for result in (creator, mutation, recover):
                _assert_no_worker_exception(self, result)
            _assert_unavailable(self, recover)
            self.assertEqual(recover["runtime"]["state"], "ACTIVATING")
            self.assertEqual(
                mutation["journal"]["private_namespace"],
                recover["journal"]["private_namespace"],
            )
            self.assertIn(
                "foreign.candidate",
                recover["journal"]["private_namespace"],
            )
            self.assertEqual(
                mutation["disk"]["database"],
                recover["runtime"]["database"],
            )
        finally:
            _remove_long_quarantine(root)
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
