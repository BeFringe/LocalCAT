"""WA-06 5.14a public Windows snapshot refresh crash/recovery acceptance."""

from __future__ import annotations

import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import tm_snapshot_recovery
from tm_contracts import SnapshotReceipt, SourceBindingState, snapshot_receipt_digest
from tests.test_tm_initial_activation_windows import _remove_long_quarantine
from tests.test_tm_portable_replacement_owner_windows import _remove_long_private
from tests.windows_tm_snapshot_recovery_worker import (
    ADAPTER_PUBLISH_EVENTS,
    ARTIFACT_ROLES,
    EXISTING_CANDIDATE_RECOVERY_PHASES,
    PUBLICATION_PHASES,
    RECOVERY_PHASES,
    RECONSTRUCTION_PHASES,
    REBOOT_TICKET_SCHEMA,
    SCHEMA,
)


WORKER = Path(__file__).with_name("windows_tm_snapshot_recovery_worker.py")

EXPECTED_PUBLICATION_PHASES = (
    *(f"candidate_flush.{edge}.{ordinal}" for ordinal in range(1, 3) for edge in ("before", "after")),
    "issued.before",
    "issued.after",
    *(
        f"adapter.{event}.{ordinal}"
        for event in ADAPTER_PUBLISH_EVENTS
        for ordinal in range(1, 5)
    ),
    *(f"retained_readback.{edge}.{ordinal}" for ordinal in range(1, 4) for edge in ("before", "after")),
    "owner_completion.before",
    "owner_completion.after",
    "business_reproof.before",
    "business_reproof.after",
    *(f"terminal_reproof.{edge}.{ordinal}" for ordinal in range(1, 5) for edge in ("before", "after")),
    *(f"close.{edge}.{ordinal}" for ordinal in range(1, 5) for edge in ("before", "after")),
)

EXPECTED_RECOVERY_PHASES = (
    "classified",
    "effect_before",
    "effect_after",
    "retire_before.jsonl_temp",
    "retire_after.jsonl_temp",
    "retire_before.manifest_temp",
    "retire_after.manifest_temp",
    "retire_before.jsonl_recovery",
    "retire_after.jsonl_recovery",
    "retire_before.manifest_recovery",
    "retire_after.manifest_recovery",
    "terminal_cleanup",
)

EXPECTED_RECONSTRUCTION_PHASES = (
    *(f"adapter.{event}.1" for event in ADAPTER_PUBLISH_EVENTS),
    "terminal_reproof.before.1",
    "terminal_reproof.after.1",
    "close.before.1",
    "close.after.1",
)


@unittest.skipUnless(sys.platform == "win32", "requires real Windows")
class WindowsTMSnapshotRecoveryProcessTests(unittest.TestCase):
    """True process death followed by public cold-open/recovery."""

    maxDiff = None

    def _command(self, mode: str, root: Path, *extra: str) -> list[str]:
        return [sys.executable, str(WORKER), mode, str(root), *extra]

    def _run_json(self, mode: str, root: Path, *extra: str) -> dict[str, object]:
        completed = subprocess.run(
            self._command(mode, root, *extra),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=180.0,
        )
        stdout = completed.stdout.decode("utf-8", errors="replace")
        stderr = completed.stderr.decode("utf-8", errors="replace")
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"worker {mode} failed\nstdout={stdout}\nstderr={stderr}",
        )
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as error:
            self.fail(f"worker {mode} returned invalid JSON: {error}: {stdout!r}")
        self.assertIs(type(result), dict)
        self.assertEqual(result.get("schema"), SCHEMA, result)
        return result

    def _wait_for_marker(
        self,
        marker: Path,
        process: subprocess.Popen[bytes],
    ) -> dict[str, object]:
        deadline = time.monotonic() + 180.0
        while not marker.is_file() or marker.stat().st_size == 0:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(
                    "worker exited before phase marker: "
                    f"{process.returncode}\n"
                    f"stdout={stdout.decode('utf-8', errors='replace')}\n"
                    f"stderr={stderr.decode('utf-8', errors='replace')}"
                )
            if time.monotonic() >= deadline:
                raise AssertionError("worker phase marker timeout")
            time.sleep(0.02)
        return json.loads(marker.read_text(encoding="utf-8"))

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        terminate = kernel32.TerminateProcess
        terminate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        terminate.restype = ctypes.c_int32
        if not terminate(int(process._handle), 94):
            raise ctypes.WinError(ctypes.get_last_error())

    def _kill_at(
        self,
        mode: str,
        root: Path,
        phase: str,
        marker: Path,
        *extra: str,
    ) -> dict[str, object]:
        process = subprocess.Popen(
            self._command(
                mode,
                root,
                "--phase",
                phase,
                "--marker",
                str(marker),
                *extra,
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        terminated = False
        try:
            observed = self._wait_for_marker(marker, process)
            self.assertEqual(observed["phase"], phase)
            self.assertEqual(observed["schema"], SCHEMA)
            self._terminate(process)
            terminated = True
            stdout, stderr = process.communicate(timeout=30.0)
            self.assertEqual(
                process.returncode,
                94,
                msg=(
                    f"unexpected terminated exit at {phase}\n"
                    f"stdout={stdout.decode('utf-8', errors='replace')}\n"
                    f"stderr={stderr.decode('utf-8', errors='replace')}"
                ),
            )
            return observed
        finally:
            if process.poll() is None:
                if not terminated:
                    self._terminate(process)
                process.communicate(timeout=30.0)

    def _case_root(self, label: str) -> tuple[tempfile.TemporaryDirectory[str], Path, Path]:
        temporary = tempfile.TemporaryDirectory(
            prefix="localcat-tm-refresh-recovery-",
            ignore_cleanup_errors=True,
        )
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        root = base / label

        def clean_private() -> None:
            if not root.exists():
                return
            try:
                _remove_long_quarantine(root)
            finally:
                _remove_long_private(root)

        self.addCleanup(clean_private)
        return temporary, root, base

    def _prepare(self, root: Path) -> dict[str, object]:
        prepared = self._run_json("prepare", root)
        snapshot = prepared["snapshot"]
        self.assertEqual(snapshot["generation"], 1)
        self.assertEqual(
            snapshot["source_state"],
            SourceBindingState.VERIFIED_HISTORY.value,
        )
        self.assertEqual(snapshot["database"]["refresh_rows"], [])
        return prepared

    def _assert_canonical_unchanged(
        self,
        baseline: dict[str, object],
        recovered: dict[str, object],
    ) -> None:
        before = baseline["snapshot"]
        after = recovered["snapshot"]
        if after["canonical_digest"] is None:
            self.assertEqual(
                after["database"]["canonical_rows_digest"],
                before["database"]["canonical_rows_digest"],
            )
            self.assertEqual(
                after["database"]["canonical_record_count"],
                before["database"]["canonical_record_count"],
            )
            self.assertEqual(
                after["database"]["head_revision"],
                before["database"]["head_revision"],
            )
            self.assertEqual(after["generation"], before["generation"])
            return
        self.assertEqual(after["canonical_digest"], before["canonical_digest"])
        self.assertEqual(after["canonical"], before["canonical"])
        self.assertEqual(after["generation"], 1)
        self.assertEqual(after["head_revision"], before["head_revision"])
        self.assertEqual(after["record_count"], before["record_count"])

    def _assert_idempotent_public_recovery(
        self,
        recovered: dict[str, object],
        expected_first: str,
        *,
        expected_second: str = "NOOP",
    ) -> None:
        first = recovered["first_recovery"]
        second = recovered["second_recovery"]
        self.assertEqual(first.get("state"), expected_first, first)
        self.assertEqual(second.get("state"), expected_second, second)

    @staticmethod
    def _phase_has_issued_receipt(phase: str) -> bool:
        return not (
            phase.startswith("candidate_flush.")
            or phase == "issued.before"
        )

    @classmethod
    def _expected_pair_kind(cls, phase: str) -> str:
        if not cls._phase_has_issued_receipt(phase):
            return "old"
        if phase == "issued.after":
            return "old"
        if phase.startswith("adapter."):
            _adapter, event, ordinal_text = phase.split(".")
            ordinal = int(ordinal_text)
            if ordinal <= 2:
                return "old"
            if event == "publish_before_rename":
                return "old" if ordinal == 3 else "jsonl-only"
            return "jsonl-only" if ordinal == 3 else "new"
        return "new"

    @classmethod
    def _expected_first_recovery(cls, phase: str) -> str:
        if not cls._phase_has_issued_receipt(phase):
            return "BLOCKED"
        if cls._phase_has_completed_receipt(phase):
            return "NOOP"
        return (
            "CANCELLED"
            if cls._expected_pair_kind(phase) == "old"
            else "COMPLETED"
        )

    @staticmethod
    def _phase_has_completed_receipt(phase: str) -> bool:
        return (
            phase in {
                "retained_readback.before.3",
                "retained_readback.after.3",
                "owner_completion.after",
                "business_reproof.before",
                "business_reproof.after",
            }
            or phase.startswith("terminal_reproof.")
            or phase.startswith("close.")
        )

    def _assert_raw_publication_state(
        self,
        phase: str,
        baseline: dict[str, object],
        raw: dict[str, object],
    ) -> None:
        before = baseline["snapshot"]
        observed = raw["snapshot"]
        rows = observed["database"]["refresh_rows"]
        if not self._phase_has_issued_receipt(phase):
            self.assertEqual(rows, [])
            self.assertEqual(
                observed["database"]["bound_refresh_handoffs"],
                [],
            )
            self.assertEqual(observed["jsonl"], before["jsonl"])
            self.assertEqual(observed["manifest"], before["manifest"])
            self.assertEqual(
                observed["database"]["binding"],
                before["database"]["binding"],
            )
            self.assertTrue(
                any(value is not None for value in observed["artifacts"].values()),
                observed["artifacts"],
            )
            return

        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(
            len(observed["database"]["bound_refresh_handoffs"]),
            1,
            observed["database"],
        )
        pair_kind = self._expected_pair_kind(phase)
        if pair_kind == "old":
            self.assertEqual(observed["jsonl"], before["jsonl"])
            self.assertEqual(observed["manifest"], before["manifest"])
            self.assertEqual(
                observed["database"]["binding"],
                before["database"]["binding"],
            )
        else:
            self.assertEqual(observed["jsonl"]["sha256"], row[2])
        if pair_kind == "jsonl-only":
            self.assertEqual(observed["manifest"], before["manifest"])
            self.assertEqual(
                observed["database"]["binding"],
                before["database"]["binding"],
            )
        elif pair_kind == "new":
            manifest_contract = observed["manifest_contract"]
            self.assertEqual(manifest_contract["receipt"]["snapshot_id"], row[0])
        completed_phase = self._phase_has_completed_receipt(phase)
        self.assertEqual(row[4], "completed" if completed_phase else "issued")
        if completed_phase:
            self.assertEqual(observed["database"]["binding"][3], row[0])
        else:
            self.assertEqual(
                observed["database"]["binding"],
                before["database"]["binding"],
            )

    def _retirement_leaf(self, row: list[object]) -> str:
        receipt = SnapshotReceipt(
            snapshot_id=str(row[0]),
            exported_revision=int(row[1]),
            jsonl_digest=str(row[2]),
            record_count=int(row[3]),
            resource_id=str(row[7]),
            canonical_store_id=str(row[8]),
            format_version=str(row[9]),
        )
        return snapshot_receipt_digest(receipt)

    def _assert_receipt_scoped_retirement(
        self,
        phase: str,
        raw: dict[str, object],
        recovered: dict[str, object],
        *,
        consumed_roles: frozenset[str] = frozenset(),
    ) -> None:
        before = raw["snapshot"]
        after = recovered["snapshot"]
        if not self._phase_has_issued_receipt(phase):
            self.assertEqual(after["artifacts"], before["artifacts"])
            self.assertEqual(after["retirement"]["receipts"], {})
            return
        self.assertTrue(
            all(value is None for value in after["artifacts"].values()),
            after["artifacts"],
        )
        self.assertEqual(after["database"]["bound_refresh_handoffs"], [])
        rows = after["database"]["refresh_rows"]
        self.assertEqual(len(rows), 1, rows)
        retirement_leaf = self._retirement_leaf(rows[0])
        receipts = after["retirement"]["receipts"]
        self.assertEqual(set(receipts), {retirement_leaf}, receipts)
        entries = receipts[retirement_leaf]["entries"]
        effective_consumed_roles = consumed_roles
        if self._expected_pair_kind(phase) == "jsonl-only":
            # Recovery publishes the already-issued manifest candidate; it is
            # consumed by the destination replace rather than retired.
            effective_consumed_roles = consumed_roles | frozenset(
                {"manifest_temp"}
            )
        expected = {
            facts["name"]: (role, facts)
            for role, facts in before["artifacts"].items()
            if facts is not None and role not in effective_consumed_roles
        }
        self.assertEqual(set(entries), set(expected), entries)
        for source_name, (role, facts) in expected.items():
            self.assertIn(role, ARTIFACT_ROLES)
            self.assertEqual(entries[source_name]["size"], facts["size"])
            self.assertEqual(entries[source_name]["sha256"], facts["sha256"])

    def _assert_old_pair(
        self,
        baseline: dict[str, object],
        recovered: dict[str, object],
        *,
        issued_expected: bool,
    ) -> None:
        before = baseline["snapshot"]
        after = recovered["snapshot"]
        self.assertEqual(after["jsonl"], before["jsonl"])
        self.assertEqual(after["manifest"], before["manifest"])
        self.assertEqual(
            after["database"]["binding"],
            before["database"]["binding"],
        )
        rows = after["database"]["refresh_rows"]
        if issued_expected:
            self.assertEqual(len(rows), 1, rows)
            self.assertEqual(rows[0][4], "cancelled", rows)
        else:
            self.assertEqual(rows, [])
        self.assertEqual(
            after["source_state"],
            (
                None
                if not issued_expected and after["canonical_digest"] is None
                else SourceBindingState.VERIFIED_HISTORY.value
            ),
        )

    def _assert_new_pair(
        self,
        recovered: dict[str, object],
        *,
        expected_source_state: str = SourceBindingState.VERIFIED_CURRENT.value,
    ) -> None:
        after = recovered["snapshot"]
        rows = after["database"]["refresh_rows"]
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row[4], "completed", rows)
        self.assertEqual(after["database"]["binding"][3], row[0])
        self.assertEqual(after["jsonl"]["sha256"], row[2])
        manifest_contract = after["manifest_contract"]
        self.assertEqual(manifest_contract["receipt"]["snapshot_id"], row[0])
        self.assertEqual(
            after["source_state"],
            expected_source_state,
        )

    def test_windows_profile_and_phase_catalog_have_no_skip_fallback(self) -> None:
        self.assertEqual(sys.platform, "win32")
        self.assertEqual(PUBLICATION_PHASES, EXPECTED_PUBLICATION_PHASES)
        self.assertEqual(RECOVERY_PHASES, EXPECTED_RECOVERY_PHASES)
        self.assertEqual(RECONSTRUCTION_PHASES, EXPECTED_RECONSTRUCTION_PHASES)
        self.assertTrue(WORKER.is_file())

    def test_bound_refresh_handoff_codec_is_exact_and_portable(self) -> None:
        facts = tm_snapshot_recovery._BoundRefreshHandoffFacts(
            snapshot_id="snapshot.refresh.codec",
            prior_snapshot_id="snapshot.prior.codec",
            prior_snapshot_kind="EXPLICIT_EXPORT",
            prior_jsonl_digest="1" * 64,
            prior_manifest_digest="2" * 64,
            new_jsonl_digest="3" * 64,
            new_manifest_digest="4" * 64,
        )
        key = tm_snapshot_recovery._bound_refresh_handoff_meta_key(
            facts.snapshot_id
        )
        encoded = tm_snapshot_recovery._bound_refresh_handoff_meta_value(facts)
        payload = json.loads(encoded)
        self.assertEqual(
            set(payload),
            {
                "version",
                "snapshot_id",
                "prior_snapshot_id",
                "prior_snapshot_kind",
                "prior_jsonl_digest",
                "prior_manifest_digest",
                "new_jsonl_digest",
                "new_manifest_digest",
            },
        )
        self.assertEqual(payload["version"], "bound-refresh-handoff-v1")
        self.assertEqual(
            tm_snapshot_recovery._bound_refresh_handoff_from_meta(key, encoded),
            facts,
        )

        malformed_payloads = []
        for mutate in (
            lambda value: {**value, "extra": True},
            lambda value: {
                name: item
                for name, item in value.items()
                if name != "new_manifest_digest"
            },
            lambda value: {**value, "version": "bound-refresh-handoff-v2"},
            lambda value: {**value, "new_jsonl_digest": "A" * 64},
            lambda value: {**value, "new_jsonl_digest": "a" * 63},
        ):
            malformed_payloads.append(
                json.dumps(mutate(payload), sort_keys=True, separators=(",", ":"))
            )
        malformed_payloads.append(
            encoded.replace(
                '\"snapshot_id\":\"snapshot.refresh.codec\"',
                '\"snapshot_id\":\"snapshot.refresh.codec\",'
                '\"snapshot_id\":\"snapshot.refresh.duplicate\"',
                1,
            )
        )
        for malformed in malformed_payloads:
            with self.subTest(payload=malformed):
                self.assertIsNone(
                    tm_snapshot_recovery._bound_refresh_handoff_from_meta(
                        key,
                        malformed,
                    )
                )
        self.assertIsNone(
            tm_snapshot_recovery._bound_refresh_handoff_from_meta(
                tm_snapshot_recovery._bound_refresh_handoff_meta_key(
                    "snapshot.refresh.other"
                ),
                encoded,
            )
        )

    def test_completed_runtime_manifest_is_recovery_observation_only(self) -> None:
        for kind, expected_state in (
            (None, SourceBindingState.VERIFIED_HISTORY.value),
            ("foreign", SourceBindingState.SOURCE_DIVERGED.value),
            ("absent", SourceBindingState.SOURCE_DIVERGED.value),
        ):
            with self.subTest(kind=kind):
                _temporary, root, _base = self._case_root(
                    "completed-manifest-" + ("unchanged" if kind is None else kind)
                )
                baseline = self._prepare(root)
                if kind is not None:
                    self._run_json(
                        "mutate-configured-manifest",
                        root,
                        "--kind",
                        kind,
                    )

                opened = self._run_json("cold-open-probe", root)

                self.assertIs(opened["opened"], True, opened)
                self.assertIsNone(opened["opening_error"])
                self.assertEqual(len(opened["recoveries"]), 1, opened)
                self.assertEqual(
                    opened["recoveries"][0].get("state"),
                    "NOOP",
                    opened,
                )
                self.assertEqual(
                    opened["snapshot"]["source_state"],
                    expected_state,
                )
                self.assertEqual(
                    opened["snapshot"]["canonical_digest"],
                    baseline["snapshot"]["canonical_digest"],
                )

    def test_completed_refresh_manifest_replace_reaches_bound_recovery(self) -> None:
        _temporary, root, base = self._case_root("completed-manifest-replace")
        baseline = self._prepare(root)
        marker = base / "completed-manifest-replace.ready.json"
        self._kill_at(
            "refresh-fault",
            root,
            "owner_completion.after",
            marker,
        )
        raw = self._run_json("inspect", root)

        opened = self._run_json("cold-open-probe", root)

        self.assertIs(opened["opened"], True, opened)
        self.assertEqual(len(opened["recoveries"]), 1, opened)
        self.assertEqual(opened["recoveries"][0].get("state"), "NOOP", opened)
        self.assertEqual(
            opened["snapshot"]["source_state"],
            SourceBindingState.VERIFIED_CURRENT.value,
        )
        self.assertEqual(opened["snapshot"]["generation"], 1)
        self.assertEqual(
            opened["snapshot"]["database"]["canonical_rows_digest"],
            baseline["snapshot"]["database"]["canonical_rows_digest"],
        )
        self.assertEqual(
            opened["snapshot"]["database"]["bound_refresh_handoffs"],
            [],
        )
        self._assert_receipt_scoped_retirement(
            "owner_completion.after",
            raw,
            opened,
        )

    def test_generation_zero_completed_refresh_cold_opens_through_recovery(
        self,
    ) -> None:
        _temporary, root, base = self._case_root("generation-zero-completed")
        baseline = self._run_json("prepare-gen0", root)
        self.assertEqual(baseline["snapshot"]["generation"], 0)
        marker = base / "generation-zero-completed.ready.json"
        self._kill_at(
            "refresh-fault",
            root,
            "owner_completion.after",
            marker,
        )
        raw = self._run_json("inspect", root)

        opened = self._run_json("cold-open-probe", root)

        self.assertIs(opened["opened"], True, opened)
        self.assertEqual(len(opened["recoveries"]), 1, opened)
        self.assertEqual(opened["recoveries"][0].get("state"), "NOOP", opened)
        self.assertEqual(opened["snapshot"]["generation"], 0)
        self.assertEqual(
            opened["snapshot"]["database"]["canonical_rows_digest"],
            baseline["snapshot"]["database"]["canonical_rows_digest"],
        )
        self.assertEqual(
            opened["snapshot"]["database"]["bound_refresh_handoffs"],
            [],
        )
        self._assert_receipt_scoped_retirement(
            "owner_completion.after",
            raw,
            opened,
        )

    def test_generation_zero_normal_refresh_cold_opens_through_recovery(
        self,
    ) -> None:
        _temporary, root, _base = self._case_root("generation-zero-normal")
        baseline = self._run_json("prepare-gen0", root)
        self.assertEqual(baseline["snapshot"]["generation"], 0)

        refreshed = self._run_json("refresh", root)

        self.assertEqual(
            refreshed["outcome"]["kind"],
            "ExportReport",
            refreshed,
        )
        self.assertEqual(refreshed["snapshot"]["generation"], 0)
        raw = self._run_json("inspect", root)
        opened = self._run_json("cold-open-probe", root)
        self.assertIs(opened["opened"], True, opened)
        self.assertEqual(len(opened["recoveries"]), 1, opened)
        self.assertEqual(opened["recoveries"][0].get("state"), "NOOP", opened)
        self.assertEqual(opened["snapshot"]["generation"], 0)
        self.assertEqual(
            opened["snapshot"]["database"]["canonical_rows_digest"],
            baseline["snapshot"]["database"]["canonical_rows_digest"],
        )
        self.assertEqual(
            opened["snapshot"]["database"]["bound_refresh_handoffs"],
            [],
        )
        self._assert_receipt_scoped_retirement(
            "owner_completion.after",
            raw,
            opened,
        )

    def test_completed_runtime_authority_tamper_stops_before_bound_recovery(
        self,
    ) -> None:
        for kind in (
            "canonical-sidecar",
            "private-lineage",
            "active-attestation",
        ):
            with self.subTest(kind=kind):
                _temporary, root, _base = self._case_root(
                    "completed-authority-" + kind
                )
                self._prepare(root)
                self._run_json(
                    "tamper-hydration-authority",
                    root,
                    "--kind",
                    kind,
                )

                rejected = self._run_json("cold-open-probe", root)

                self.assertIs(rejected["opened"], False, rejected)
                self.assertEqual(rejected["recoveries"], [], rejected)
                error = rejected["opening_error"]
                self.assertEqual(error["type"], "ValueError", rejected)
                self.assertTrue(
                    error["message"].startswith("TM.CANONICAL_"),
                    rejected,
                )

    def test_publication_process_death_matrix_converges_old_or_new(self) -> None:
        for phase in EXPECTED_PUBLICATION_PHASES:
            with self.subTest(phase=phase):
                _temporary, root, base = self._case_root(phase.replace(".", "-"))
                baseline = self._prepare(root)
                marker = base / f"{phase}.ready.json"
                self._kill_at("refresh-fault", root, phase, marker)
                raw = self._run_json("inspect", root)
                self._assert_raw_publication_state(phase, baseline, raw)
                recovered = self._run_json("recover", root)
                self._assert_canonical_unchanged(baseline, recovered)
                expected_first = self._expected_first_recovery(phase)
                if not self._phase_has_issued_receipt(phase):
                    self._assert_old_pair(
                        baseline,
                        recovered,
                        issued_expected=False,
                    )
                    self._assert_idempotent_public_recovery(
                        recovered,
                        expected_first,
                        expected_second="BLOCKED",
                    )
                elif expected_first == "CANCELLED":
                    self._assert_old_pair(
                        baseline,
                        recovered,
                        issued_expected=True,
                    )
                    self._assert_idempotent_public_recovery(
                        recovered,
                        expected_first,
                    )
                else:
                    self._assert_new_pair(recovered)
                    self._assert_idempotent_public_recovery(
                        recovered,
                        expected_first,
                    )
                self._assert_receipt_scoped_retirement(
                    phase,
                    raw,
                    recovered,
                )

    def test_unmatched_pair_latches_divergence_without_canonical_change(self) -> None:
        _temporary, root, base = self._case_root("unmatched")
        baseline = self._prepare(root)
        marker = base / "issued.ready.json"
        self._kill_at("refresh-fault", root, "issued.after", marker)
        self._run_json("mutate-unmatched", root)

        recovered = self._run_json("recover", root)

        self._assert_canonical_unchanged(baseline, recovered)
        snapshot = recovered["snapshot"]
        self.assertEqual(
            snapshot["source_state"],
            SourceBindingState.SOURCE_DIVERGED.value,
        )
        self.assertEqual(snapshot["database"]["divergence_latched"], "1")
        first = recovered["first_recovery"]
        self.assertEqual(first.get("state"), "DIVERGED", first)
        self.assertEqual(first.get("diagnostics"), ["RECOVERY.PAIR_UNMATCHED"], first)
        second = recovered["second_recovery"]
        self.assertEqual(second.get("state"), "NOOP", second)
        self.assertEqual(
            second.get("diagnostics"),
            ["RECOVERY.DIVERGENCE_PRESERVED"],
            second,
        )
        self.assertEqual(
            recovered["durable_before_open"]["database"]["divergence_latched"],
            "0",
        )
        self.assertEqual(
            recovered["durable_after_open"]["database"]["divergence_latched"],
            "1",
        )
        self.assertEqual(
            recovered["durable_after_observation"]["database"]["divergence_latched"],
            "1",
        )
        self.assertNotEqual(snapshot["jsonl"], baseline["snapshot"]["jsonl"])

    def test_unmatched_pair_with_unknown_retirement_blocks_before_divergence(
        self,
    ) -> None:
        _temporary, root, base = self._case_root(
            "unmatched-retirement-unknown"
        )
        baseline = self._prepare(root)
        marker = base / "unmatched-retirement-unknown.ready.json"
        self._kill_at("refresh-fault", root, "issued.after", marker)
        self._run_json("mutate-unmatched", root)
        self._run_json("mutate-retirement-unknown", root)
        before = self._run_json("inspect", root)

        blocked = self._run_json("recover", root)

        self._assert_canonical_unchanged(baseline, blocked)
        first = blocked["first_recovery"]
        second = blocked["second_recovery"]
        self.assertEqual(first.get("state"), "BLOCKED", first)
        self.assertEqual(first.get("error_code"), "RECOVERY.ARTIFACT_CONFLICT", first)
        self.assertEqual(second.get("state"), "BLOCKED", second)
        self.assertEqual(second.get("error_code"), "RECOVERY.ARTIFACT_CONFLICT", second)
        after = blocked["snapshot"]
        self.assertEqual(after["database"]["divergence_latched"], "0")
        self.assertEqual(after["database"], before["snapshot"]["database"])
        self.assertEqual(after["artifacts"], before["snapshot"]["artifacts"])
        self.assertEqual(after["retirement"], before["snapshot"]["retirement"])

    def test_multiple_bound_refresh_rows_block_without_mutation(self) -> None:
        _temporary, root, base = self._case_root("multiple-bound-rows")
        baseline = self._prepare(root)
        marker = base / "multiple-bound-rows.ready.json"
        self._kill_at("refresh-fault", root, "issued.after", marker)
        self._run_json("tamper-multiple-bound-rows", root)
        before = self._run_json("inspect", root)

        blocked = self._run_json("recover", root)

        self._assert_canonical_unchanged(baseline, blocked)
        for outcome_name in ("first_recovery", "second_recovery"):
            outcome = blocked[outcome_name]
            self.assertEqual(outcome.get("state"), "BLOCKED", outcome)
            self.assertEqual(
                outcome.get("error_code"),
                "RECOVERY.BOUND_HANDOFF_AMBIGUOUS",
                outcome,
            )
        after = blocked["snapshot"]
        self.assertEqual(after["database"], before["snapshot"]["database"])
        self.assertEqual(after["database"]["divergence_latched"], "0")
        self.assertEqual(after["artifacts"], before["snapshot"]["artifacts"])
        self.assertEqual(after["retirement"], before["snapshot"]["retirement"])

    def test_configured_legacy_and_bound_handoffs_block_without_mutation(
        self,
    ) -> None:
        _temporary, root, base = self._case_root("legacy-bound-ambiguity")
        self._prepare(root)
        self._kill_at(
            "refresh-fault",
            root,
            "owner_completion.after",
            base / "legacy-bound-ambiguity.ready.json",
        )
        injected = self._run_json(
            "configured-legacy-handoff-ambiguity", root
        )
        before = injected["snapshot"]
        database = before["database"]
        self.assertEqual(len(database["bound_refresh_handoffs"]), 1)
        self.assertEqual(len(database["artifact_handoffs"]), 1)
        self.assertEqual(len(database["refresh_rows"]), 2)
        self.assertNotEqual(
            injected["bound_snapshot_id"], injected["legacy_snapshot_id"]
        )
        destinations = {
            (row[5], row[6]) for row in database["refresh_rows"]
        }
        self.assertEqual(len(destinations), 1)

        blocked = self._run_json("recover", root)
        for outcome_name in ("first_recovery", "second_recovery"):
            outcome = blocked[outcome_name]
            self.assertEqual(outcome.get("state"), "BLOCKED", outcome)
            self.assertEqual(
                outcome.get("error_code"),
                "RECOVERY.BOUND_HANDOFF_AMBIGUOUS",
                outcome,
            )
        self.assertEqual(blocked["durable_before_open"], before)
        self.assertEqual(blocked["durable_after_open"], before)
        self.assertEqual(blocked["durable_after_observation"], before)
        self.assertEqual(blocked["snapshot"]["database"], database)
        self.assertEqual(blocked["snapshot"]["artifacts"], before["artifacts"])
        self.assertEqual(blocked["snapshot"]["retirement"], before["retirement"])
        self.assertEqual(blocked["snapshot"]["jsonl"], before["jsonl"])
        self.assertEqual(blocked["snapshot"]["manifest"], before["manifest"])

    def test_stable_absent_pair_classification_is_not_unsafe(self) -> None:
        # New JSONL plus a stably absent manifest is the one reconstructable
        # absent state.  Old/foreign/missing JSONL shapes are stable evidence,
        # but outside both old/new closures and therefore diverge rather than
        # being mislabeled as an unobservable/unsafe platform failure.
        _temporary, root, base = self._case_root("new-jsonl-manifest-absent")
        baseline = self._prepare(root)
        seed_phase = "adapter.publish_after_destination_reopen.3"
        marker = base / "new-jsonl-manifest-absent.ready.json"
        self._kill_at("refresh-fault", root, seed_phase, marker)
        self._run_json(
            "mutate-pair",
            root,
            "--kind",
            "manifest-absent",
        )
        raw = self._run_json("inspect", root)

        reconstructed = self._run_json("recover", root)

        self._assert_canonical_unchanged(baseline, reconstructed)
        self._assert_new_pair(reconstructed)
        self._assert_idempotent_public_recovery(reconstructed, "COMPLETED")
        self._assert_receipt_scoped_retirement(
            seed_phase,
            raw,
            reconstructed,
        )

        for kind in (
            "manifest-absent",
            "foreign-jsonl-manifest-absent",
            "jsonl-absent",
            "both-absent",
        ):
            with self.subTest(kind=kind):
                _temporary, root, base = self._case_root(
                    "diverged-stable-absent-" + kind
                )
                baseline = self._prepare(root)
                marker = base / f"{kind}.ready.json"
                self._kill_at("refresh-fault", root, "issued.after", marker)
                self._run_json("mutate-pair", root, "--kind", kind)
                before = self._run_json("inspect", root)

                diverged = self._run_json("recover", root)

                self._assert_canonical_unchanged(baseline, diverged)
                first = diverged["first_recovery"]
                second = diverged["second_recovery"]
                self.assertEqual(first.get("state"), "DIVERGED", first)
                self.assertEqual(
                    first.get("diagnostics"),
                    ["RECOVERY.PAIR_UNMATCHED"],
                    first,
                )
                self.assertEqual(second.get("state"), "NOOP", second)
                self.assertEqual(
                    second.get("diagnostics"),
                    ["RECOVERY.DIVERGENCE_PRESERVED"],
                    second,
                )
                after = diverged["snapshot"]
                self.assertEqual(after["database"]["divergence_latched"], "1")
                self.assertEqual(after["jsonl"], before["snapshot"]["jsonl"])
                self.assertEqual(
                    after["manifest"],
                    before["snapshot"]["manifest"],
                )
                self.assertEqual(
                    after["database"]["bound_refresh_handoffs"],
                    before["snapshot"]["database"]["bound_refresh_handoffs"],
                )
                self.assertEqual(after["artifacts"], before["snapshot"]["artifacts"])
                self.assertEqual(
                    after["retirement"],
                    before["snapshot"]["retirement"],
                )

    def test_issued_pair_can_complete_as_verified_history_after_head_advance(
        self,
    ) -> None:
        _temporary, root, base = self._case_root("issued-history")
        baseline = self._prepare(root)
        marker = base / "issued-history.ready.json"
        phase = "adapter.publish_after_destination_reopen.3"
        self._kill_at(
            "refresh-fault",
            root,
            phase,
            marker,
            "--advance-head-after-issued",
        )
        raw = self._run_json("inspect", root)
        before_database = raw["snapshot"]["database"]
        rows = before_database["refresh_rows"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(
            before_database["head_revision"],
            baseline["snapshot"]["head_revision"] + 1,
        )
        self.assertEqual(rows[0][1], baseline["snapshot"]["head_revision"])
        canonical_rows_digest = before_database["canonical_rows_digest"]
        canonical_record_count = before_database["canonical_record_count"]

        recovered = self._run_json("recover", root)

        after = recovered["snapshot"]
        self.assertEqual(after["generation"], 1)
        self.assertEqual(
            after["database"]["canonical_rows_digest"],
            canonical_rows_digest,
        )
        self.assertEqual(
            after["database"]["canonical_record_count"],
            canonical_record_count,
        )
        self.assertEqual(after["database"]["refresh_rows"][0][4], "completed")
        self.assertEqual(
            after["source_state"],
            SourceBindingState.VERIFIED_HISTORY.value,
        )
        self._assert_idempotent_public_recovery(recovered, "COMPLETED")
        self._assert_receipt_scoped_retirement(phase, raw, recovered)

    def test_unsafe_pair_blocks_without_latch_or_retirement(self) -> None:
        _temporary, root, base = self._case_root("unsafe-pair")
        baseline = self._prepare(root)
        marker = base / "unsafe-pair.ready.json"
        self._kill_at("refresh-fault", root, "issued.after", marker)
        self._run_json(
            "mutate-pair",
            root,
            "--kind",
            "jsonl-unsafe-directory",
        )
        before = self._run_json("inspect", root)

        blocked = self._run_json("recover", root)

        self._assert_canonical_unchanged(baseline, blocked)
        first = blocked["first_recovery"]
        second = blocked["second_recovery"]
        self.assertEqual(first.get("state"), "BLOCKED", first)
        self.assertEqual(first.get("diagnostics"), ["RECOVERY.PAIR_UNSAFE"], first)
        self.assertEqual(second.get("state"), "BLOCKED", second)
        self.assertEqual(second.get("diagnostics"), ["RECOVERY.PAIR_UNSAFE"], second)
        after = blocked["snapshot"]
        self.assertEqual(after["database"]["divergence_latched"], "0")
        self.assertEqual(after["jsonl"]["kind"], "unsafe")
        self.assertEqual(after["database"], before["snapshot"]["database"])
        self.assertEqual(after["artifacts"], before["snapshot"]["artifacts"])
        self.assertEqual(after["retirement"], before["snapshot"]["retirement"])

    def test_receipt_scoped_unknown_collision_blocks_without_retire_or_clear(
        self,
    ) -> None:
        _temporary, root, base = self._case_root("unknown-retirement")
        baseline = self._prepare(root)
        marker = base / "unknown-retirement.ready.json"
        self._kill_at("refresh-fault", root, "issued.after", marker)
        collision = self._run_json("mutate-retirement-unknown", root)
        raw = self._run_json("inspect", root)

        recovered = self._run_json("recover", root)

        self._assert_canonical_unchanged(baseline, recovered)
        first = recovered["first_recovery"]
        second = recovered["second_recovery"]
        self.assertEqual(first.get("state"), "BLOCKED", first)
        self.assertEqual(second.get("state"), "BLOCKED", second)
        self.assertEqual(
            first.get("error_code"),
            "RECOVERY.ARTIFACT_CONFLICT",
            first,
        )
        self.assertEqual(
            second.get("error_code"),
            "RECOVERY.ARTIFACT_CONFLICT",
            second,
        )
        before = raw["snapshot"]
        after = recovered["snapshot"]
        self.assertEqual(after["database"]["refresh_rows"], before["database"]["refresh_rows"])
        self.assertEqual(
            after["database"]["bound_refresh_handoffs"],
            before["database"]["bound_refresh_handoffs"],
        )
        self.assertEqual(after["database"]["divergence_latched"], "0")
        self.assertEqual(after["artifacts"], before["artifacts"])
        self.assertEqual(after["retirement"], before["retirement"])
        retirement_leaf = collision["retirement_leaf"]
        foreign = after["retirement"]["receipts"][retirement_leaf]["entries"]["foreign.bin"]
        self.assertEqual(
            foreign["sha256"],
            hashlib.sha256(b"foreign-retirement-collision\n").hexdigest(),
        )

    def test_reboot_ticket_same_session_never_claims_reboot_pass(self) -> None:
        _temporary, root, base = self._case_root("reboot-ticket")
        ticket = base / "tm-refresh-reboot-ticket.json"

        prepared = self._run_json(
            "reboot-prepare",
            root,
            "--ticket",
            str(ticket),
        )
        self.assertEqual(prepared["reboot"], "PREPARED")
        self.assertEqual(
            json.loads(ticket.read_text(encoding="utf-8"))["schema"],
            REBOOT_TICKET_SCHEMA,
        )

        same_session = self._run_json(
            "reboot-resume",
            root,
            "--ticket",
            str(ticket),
        )
        self.assertEqual(same_session["reboot"], "NOT_RUN")
        self.assertNotEqual(same_session.get("reboot"), "PASS")

    def test_classify_to_latch_pair_change_blocks_and_does_not_latch(self) -> None:
        _temporary, root, base = self._case_root("latch-race")
        baseline = self._prepare(root)
        marker = base / "latch-race.ready.json"
        self._kill_at("refresh-fault", root, "issued.after", marker)
        pair_backup = base / "latch-race-prior-pair"
        self._run_json(
            "mutate-unmatched",
            root,
            "--backup-pair",
            str(pair_backup),
        )

        raced = self._run_json(
            "recover-race",
            root,
            "--pair",
            str(pair_backup),
        )

        first = raced["first_recovery"]
        self.assertEqual(first.get("state"), "BLOCKED", first)
        self.assertTrue(first.get("retryable"), first)
        self.assertIn("RECOVERY.PAIR_CHANGED", first.get("diagnostics", []), first)
        after = raced["snapshot"]
        self.assertEqual(after["database"]["divergence_latched"], "0")
        self.assertEqual(after["database"]["refresh_rows"][0][4], "issued")
        self.assertEqual(
            len(after["database"]["bound_refresh_handoffs"]),
            1,
        )
        self.assertEqual(after["retirement"]["receipts"], {})
        self.assertEqual(after["jsonl"], baseline["snapshot"]["jsonl"])
        self.assertEqual(after["manifest"], baseline["snapshot"]["manifest"])

        retried = self._run_json("recover", root)
        self._assert_canonical_unchanged(baseline, retried)
        self._assert_old_pair(baseline, retried, issued_expected=True)
        self._assert_idempotent_public_recovery(retried, "CANCELLED")

    def test_classified_owner_facts_are_reproved_inside_every_sqlite_effect(
        self,
    ) -> None:
        seeds = (
            ("cancel", "issued.after"),
            ("complete", "adapter.publish_after_destination_reopen.4"),
            ("diverged", "issued.after"),
            ("clear", "owner_completion.after"),
        )
        mutations = (
            "receipt",
            "second-receipt",
            "prior",
            "id",
            "kind",
            "digest",
            "whole",
        )
        for action, seed_phase in seeds:
            for mutation in mutations:
                with self.subTest(action=action, mutation=mutation):
                    _temporary, root, base = self._case_root(
                        f"owner-race-{action}-{mutation}"
                    )
                    baseline = self._prepare(root)
                    self._kill_at(
                        "refresh-fault",
                        root,
                        seed_phase,
                        base / f"{action}-{mutation}.ready.json",
                    )
                    if action == "diverged":
                        self._run_json("mutate-unmatched", root)

                    raced = self._run_json(
                        "recover-owner-race",
                        root,
                        "--action",
                        action,
                        "--mutation",
                        mutation,
                    )

                    injected = raced["owner_race"]
                    self.assertEqual(injected["action"], action)
                    self.assertEqual(injected["mutation"], mutation)
                    first = raced["first_recovery"]
                    self.assertEqual(first.get("state"), "BLOCKED", first)
                    self.assertIsNone(raced["second_recovery"])
                    self._assert_canonical_unchanged(baseline, raced)
                    before_effect = injected["snapshot"]
                    after = raced["snapshot"]
                    self.assertEqual(after["database"], before_effect["database"])
                    self.assertEqual(after["jsonl"], before_effect["jsonl"])
                    self.assertEqual(after["manifest"], before_effect["manifest"])
                    self.assertEqual(after["artifacts"], before_effect["artifacts"])
                    self.assertEqual(
                        after["retirement"],
                        before_effect["retirement"],
                    )

    def test_terminal_handoff_tamper_never_retires_or_clears(self) -> None:
        for kind in ("prior-link", "new-digest", "unknown-target"):
            with self.subTest(kind=kind):
                _temporary, root, base = self._case_root(
                    "terminal-tamper-" + kind
                )
                baseline = self._prepare(root)
                marker = base / f"terminal-tamper-{kind}.ready.json"
                self._kill_at(
                    "refresh-fault",
                    root,
                    "owner_completion.after",
                    marker,
                )
                self._run_json(
                    "tamper-terminal-handoff",
                    root,
                    "--kind",
                    kind,
                )
                raw = self._run_json("inspect", root)

                recovered = self._run_json("recover", root)

                self._assert_canonical_unchanged(baseline, recovered)
                self.assertEqual(
                    recovered["first_recovery"].get("state"),
                    "BLOCKED",
                    recovered["first_recovery"],
                )
                self.assertEqual(
                    recovered["second_recovery"].get("state"),
                    "BLOCKED",
                    recovered["second_recovery"],
                )
                if kind == "unknown-target":
                    self.assertEqual(
                        recovered["first_recovery"].get("error_code"),
                        "RECOVERY.ARTIFACT_CONFLICT",
                    )
                    self.assertEqual(
                        recovered["second_recovery"].get("error_code"),
                        "RECOVERY.ARTIFACT_CONFLICT",
                    )
                before = raw["snapshot"]
                after = recovered["snapshot"]
                self.assertEqual(
                    after["database"]["refresh_rows"],
                    before["database"]["refresh_rows"],
                )
                self.assertEqual(
                    after["database"]["bound_refresh_handoffs"],
                    before["database"]["bound_refresh_handoffs"],
                )
                self.assertEqual(after["database"]["divergence_latched"], "0")
                self.assertEqual(after["artifacts"], before["artifacts"])
                self.assertEqual(after["retirement"], before["retirement"])

    def test_terminal_binding_invalid_blocks_without_retire_clear_or_latch(
        self,
    ) -> None:
        for kind in ("path", "status"):
            with self.subTest(kind=kind):
                _temporary, root, base = self._case_root(
                    "terminal-binding-invalid-" + kind
                )
                baseline = self._prepare(root)
                marker = base / f"terminal-binding-invalid-{kind}.ready.json"
                self._kill_at(
                    "refresh-fault",
                    root,
                    "owner_completion.after",
                    marker,
                )
                self._run_json(
                    "tamper-terminal-binding",
                    root,
                    "--kind",
                    kind,
                )
                raw = self._run_json("inspect", root)

                blocked = self._run_json("recover", root)

                self._assert_canonical_unchanged(baseline, blocked)
                for outcome_name in ("first_recovery", "second_recovery"):
                    outcome = blocked[outcome_name]
                    self.assertEqual(outcome.get("state"), "BLOCKED", outcome)
                    self.assertEqual(
                        outcome.get("error_code"),
                        "RECOVERY.BINDING_INVALID",
                        outcome,
                    )
                before = raw["snapshot"]
                after = blocked["snapshot"]
                self.assertEqual(after["database"], before["database"])
                self.assertEqual(after["database"]["divergence_latched"], "0")
                self.assertEqual(after["artifacts"], before["artifacts"])
                self.assertEqual(after["retirement"], before["retirement"])

    def test_recovery_process_death_replays_every_bound_owner_phase(self) -> None:
        seed_by_recovery_phase = {
            phase: (
                "owner_completion.after"
                if phase
                in {
                    "retire_before.jsonl_recovery",
                    "retire_after.jsonl_recovery",
                    "retire_before.manifest_recovery",
                    "retire_after.manifest_recovery",
                    "terminal_cleanup",
                }
                else "issued.after"
            )
            for phase in EXPECTED_RECOVERY_PHASES
        }
        for phase, seed_phase in seed_by_recovery_phase.items():
            with self.subTest(phase=phase, seed_phase=seed_phase):
                _temporary, root, base = self._case_root(
                    "recovery-" + phase.replace(".", "-")
                )
                baseline = self._prepare(root)
                publish_marker = base / f"{phase}.publish.ready.json"
                self._kill_at(
                    "refresh-fault",
                    root,
                    seed_phase,
                    publish_marker,
                )
                recovery_marker = base / f"{phase}.recovery.ready.json"
                self._kill_at(
                    "recover-fault",
                    root,
                    phase,
                    recovery_marker,
                )

                recovered = self._run_json("recover", root)

                self._assert_canonical_unchanged(baseline, recovered)
                if seed_phase == "issued.after":
                    self._assert_old_pair(
                        baseline,
                        recovered,
                        issued_expected=True,
                    )
                    self._assert_idempotent_public_recovery(
                        recovered,
                        (
                            "NOOP"
                            if phase == "effect_after"
                            or phase.startswith("retire_")
                            else "CANCELLED"
                        ),
                    )
                else:
                    self._assert_new_pair(recovered)
                    self._assert_idempotent_public_recovery(
                        recovered,
                        "NOOP",
                    )

    def test_manifest_reconstruction_reuses_source_candidate_across_two_crashes(
        self,
    ) -> None:
        _temporary, root, base = self._case_root(
            "reconstruct-existing-candidate-twice"
        )
        baseline = self._prepare(root)
        seed_phase = "adapter.publish_after_destination_reopen.3"
        self._kill_at(
            "refresh-fault",
            root,
            seed_phase,
            base / "existing-candidate.seed.ready.json",
        )
        seeded = self._run_json("inspect", root)
        candidate = seeded["snapshot"]["artifacts"]["manifest_temp"]
        self.assertIsNotNone(candidate, seeded)
        self.assertEqual(seeded["snapshot"]["retirement"]["receipts"], {})
        row = seeded["snapshot"]["database"]["refresh_rows"][0]
        retirement_leaf = self._retirement_leaf(row)

        phase = "adapter.publish_before_rename.1"
        for ordinal in (1, 2):
            self._kill_at(
                "reconstruct-fault",
                root,
                phase,
                base / f"existing-candidate.{ordinal}.ready.json",
            )
            raw = self._run_json("inspect", root)
            self.assertEqual(
                raw["snapshot"]["artifacts"]["manifest_temp"],
                candidate,
            )
            self.assertEqual(
                set(raw["snapshot"]["retirement"]["receipts"]),
                {retirement_leaf},
            )
            self.assertEqual(
                raw["snapshot"]["retirement"]["receipts"][retirement_leaf][
                    "entries"
                ],
                {},
            )
            self.assertEqual(
                raw["snapshot"]["database"],
                seeded["snapshot"]["database"],
            )

        recovered = self._run_json("recover", root)
        self._assert_canonical_unchanged(baseline, recovered)
        self._assert_new_pair(recovered)
        self._assert_idempotent_public_recovery(recovered, "COMPLETED")
        self._assert_receipt_scoped_retirement(
            seed_phase,
            seeded,
            recovered,
            consumed_roles=frozenset({"manifest_temp"}),
        )

    def test_manifest_reconstruction_target_only_replays_to_completion(
        self,
    ) -> None:
        _temporary, root, base = self._case_root(
            "reconstruct-target-only"
        )
        baseline = self._prepare(root)
        seed_phase = "adapter.publish_after_destination_reopen.3"
        self._kill_at(
            "refresh-fault",
            root,
            seed_phase,
            base / "target-only.seed.ready.json",
        )
        seeded = self._run_json("inspect", root)
        phase = "adapter.publish_after_rename.1"
        self._kill_at(
            "reconstruct-fault",
            root,
            phase,
            base / "target-only.reconstruct.ready.json",
        )
        raw = self._run_json("inspect", root)
        self.assertIsNone(raw["snapshot"]["artifacts"]["manifest_temp"])
        row = raw["snapshot"]["database"]["refresh_rows"][0]
        self.assertEqual(row[4], "issued", raw)
        self.assertEqual(
            raw["snapshot"]["manifest_contract"]["receipt"]["snapshot_id"],
            row[0],
        )

        recovered = self._run_json("recover", root)
        self._assert_canonical_unchanged(baseline, recovered)
        self._assert_new_pair(recovered)
        self._assert_idempotent_public_recovery(recovered, "COMPLETED")
        self._assert_receipt_scoped_retirement(
            seed_phase,
            seeded,
            recovered,
            consumed_roles=frozenset({"manifest_temp"}),
        )

    def test_manifest_reconstruction_real_retirement_target_only_replays(
        self,
    ) -> None:
        seed_phase = "adapter.publish_after_destination_reopen.3"
        for index, phase in enumerate(
            (None, *EXISTING_CANDIDATE_RECOVERY_PHASES)
        ):
            label = "nofault" if phase is None else phase
            with self.subTest(phase=label):
                _temporary, root, base = self._case_root(f"rt-{index}")
                baseline = self._prepare(root)
                self._kill_at(
                    "refresh-fault",
                    root,
                    seed_phase,
                    base / f"rt-{index}-seed.ready.json",
                )
                seeded = self._run_json("inspect", root)
                expected_candidate = seeded["snapshot"]["artifacts"][
                    "manifest_temp"
                ]
                self.assertIsNotNone(expected_candidate, seeded)
                moved = self._run_json(
                    "move-reconstruction-candidate-to-retirement",
                    root,
                )
                target_only = moved["snapshot"]
                leaf = moved["retirement_leaf"]
                self.assertIsNone(target_only["artifacts"]["manifest_temp"])
                target = target_only["retirement"]["receipts"][leaf][
                    "entries"
                ][expected_candidate["name"]]
                self.assertEqual(target["size"], expected_candidate["size"])
                self.assertEqual(target["sha256"], expected_candidate["sha256"])
                self.assertEqual(
                    target_only["manifest"],
                    baseline["snapshot"]["manifest"],
                )

                if phase is not None:
                    self._kill_at(
                        "recover-existing-candidate-fault",
                        root,
                        phase,
                        base / f"rt-{index}-recovery.ready.json",
                    )
                    interrupted = self._run_json("inspect", root)["snapshot"]
                    source = interrupted["artifacts"]["manifest_temp"]
                    retirement_entries = interrupted["retirement"]["receipts"][
                        leaf
                    ]["entries"]
                    retired = retirement_entries.get(expected_candidate["name"])
                    self.assertNotEqual(source is None, retired is None)
                    remaining = source if source is not None else retired
                    self.assertEqual(remaining["size"], expected_candidate["size"])
                    self.assertEqual(
                        remaining["sha256"], expected_candidate["sha256"]
                    )
                    self.assertEqual(
                        interrupted["manifest"],
                        baseline["snapshot"]["manifest"],
                    )
                    self.assertEqual(
                        interrupted["database"]["refresh_rows"][0][4],
                        "issued",
                    )
                    self.assertEqual(
                        len(interrupted["database"]["bound_refresh_handoffs"]),
                        1,
                    )

                recovered = self._run_json("recover", root)
                self._assert_canonical_unchanged(baseline, recovered)
                self._assert_new_pair(recovered)
                self._assert_idempotent_public_recovery(recovered, "COMPLETED")
                self._assert_receipt_scoped_retirement(
                    seed_phase,
                    seeded,
                    recovered,
                    consumed_roles=frozenset({"manifest_temp"}),
                )

    def test_manifest_reconstruction_source_and_retirement_twin_blocks(
        self,
    ) -> None:
        _temporary, root, base = self._case_root(
            "reconstruct-source-retirement-twin"
        )
        baseline = self._prepare(root)
        seed_phase = "adapter.publish_after_destination_reopen.3"
        self._kill_at(
            "refresh-fault",
            root,
            seed_phase,
            base / "source-retirement-twin.seed.ready.json",
        )
        self._kill_at(
            "reconstruct-fault",
            root,
            "adapter.publish_before_rename.1",
            base / "source-retirement-twin.reconstruct.ready.json",
        )
        collision = self._run_json(
            "mutate-reconstruction-candidate-retirement-collision",
            root,
        )
        before = collision["snapshot"]
        candidate = before["artifacts"]["manifest_temp"]
        self.assertIsNotNone(candidate, before)
        leaf = collision["retirement_leaf"]
        target = before["retirement"]["receipts"][leaf]["entries"][
            candidate["name"]
        ]
        self.assertEqual(target["size"], candidate["size"])
        self.assertEqual(target["sha256"], candidate["sha256"])

        blocked = self._run_json("recover", root)
        self._assert_canonical_unchanged(baseline, blocked)
        for outcome_name in ("first_recovery", "second_recovery"):
            outcome = blocked[outcome_name]
            self.assertEqual(outcome.get("state"), "BLOCKED", outcome)
            self.assertEqual(
                outcome.get("error_code"),
                "RECOVERY.ARTIFACT_CONFLICT",
                outcome,
            )
        after = blocked["snapshot"]
        self.assertEqual(after["database"], before["database"])
        self.assertEqual(after["database"]["divergence_latched"], "0")
        self.assertEqual(after["artifacts"], before["artifacts"])
        self.assertEqual(after["retirement"], before["retirement"])

    def test_manifest_reconstruction_uses_one_pending_publication_matrix(
        self,
    ) -> None:
        seed_phase = "adapter.publish_after_destination_reopen.3"
        for phase in EXPECTED_RECONSTRUCTION_PHASES:
            with self.subTest(phase=phase):
                _temporary, root, base = self._case_root(
                    "reconstruct-" + phase.replace(".", "-")
                )
                baseline = self._prepare(root)
                seed_marker = base / f"{phase}.seed.ready.json"
                self._kill_at(
                    "refresh-fault",
                    root,
                    seed_phase,
                    seed_marker,
                )
                seeded = self._run_json("inspect", root)
                self.assertEqual(
                    self._expected_pair_kind(seed_phase),
                    "jsonl-only",
                )
                recovery_marker = base / f"{phase}.reconstruct.ready.json"
                self._kill_at(
                    "reconstruct-fault",
                    root,
                    phase,
                    recovery_marker,
                )
                raw = self._run_json("inspect", root)

                recovered = self._run_json("recover", root)

                self._assert_canonical_unchanged(baseline, recovered)
                expected_first = (
                    "NOOP" if phase in {"close.before.1", "close.after.1"}
                    else "COMPLETED"
                )
                if expected_first == "NOOP":
                    self.assertEqual(
                        raw["snapshot"]["database"]["refresh_rows"][0][4],
                        "completed",
                    )
                    self.assertEqual(
                        len(
                            raw["snapshot"]["database"][
                                "bound_refresh_handoffs"
                            ]
                        ),
                        1,
                    )
                self._assert_new_pair(recovered)
                self._assert_idempotent_public_recovery(
                    recovered,
                    expected_first,
                )
                self._assert_receipt_scoped_retirement(
                    seed_phase,
                    seeded,
                    recovered,
                    consumed_roles=frozenset({"manifest_temp"}),
                )
                self.assertEqual(
                    seeded["snapshot"]["database"]["refresh_rows"][0][0],
                    recovered["snapshot"]["database"]["refresh_rows"][0][0],
                )


if __name__ == "__main__":
    unittest.main()
