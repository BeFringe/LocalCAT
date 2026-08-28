"""Real-process WindowsDocumentedPublishV1 recovery evidence worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platform_fs_contracts import LockPolicy, LockWait, PublishMode
from platform_fs_windows import WindowsPlatformAdapter
from tools.windows_c3b_source_snapshot import source_snapshot_sha256


SCHEMA = "localcat.windows-documented-publish-recovery.v1"
TICKET_SCHEMA = "localcat.windows-documented-publish-reboot-ticket.v1"
OLD_PAYLOAD = b"documented-publish-old\n"
NEW_PAYLOAD = b"documented-publish-new\n"
LOCK_PAYLOAD = (
    b"LOCALCAT-PROTOCOL-CONTROL-LOCK\x00"
    b"schema=1\nresource-family=documented-publish\nrange-map=exclusive-byte-0\n"
)
FAULT_PHASES = (
    "candidate_after_create",
    "candidate_after_write",
    "candidate_after_flush",
    "publish_before_rename",
    "publish_after_rename",
    "publish_after_final_facts",
    "publish_before_destination_reopen",
    "publish_after_destination_reopen",
    "owner_before_commit",
    "owner_after_commit",
    "owner_after_terminal",
)
EXPECTED_RECOVERY = {
    "candidate_after_create": "OLD",
    "candidate_after_write": "OLD",
    "candidate_after_flush": "OLD",
    "publish_before_rename": "OLD",
    "publish_after_rename": "RECOVERY_REQUIRED",
    "publish_after_final_facts": "RECOVERY_REQUIRED",
    "publish_before_destination_reopen": "RECOVERY_REQUIRED",
    "publish_after_destination_reopen": "RECOVERY_REQUIRED",
    "owner_before_commit": "RECOVERY_REQUIRED",
    "owner_after_commit": "NEW",
    "owner_after_terminal": "NEW",
}


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _owner_state(payload: bytes, generation: str) -> bytes:
    return _canonical_json(
        {
            "content_sha256": hashlib.sha256(payload).hexdigest(),
            "generation": generation,
            "phase": "COMMITTED",
            "schema": SCHEMA,
        }
    )


def _write_seed(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def seed(root_path: Path) -> None:
    root_path.mkdir(parents=True, exist_ok=False)
    _write_seed(root_path / "canonical.bin", OLD_PAYLOAD)
    _write_seed(root_path / "owner-state.json", _owner_state(OLD_PAYLOAD, "old"))


def _trigger(selected: str | None, phase: str, fault_kind: str | None) -> None:
    if selected != phase:
        return
    if fault_kind == "terminate":
        os._exit(87)
    if fault_kind == "instruction":
        raise RuntimeError(f"injected instruction fault at {phase}")
    raise AssertionError("fault kind is required for a selected phase")


def _publish_state(parent: object, lease: object, payload: bytes) -> None:
    candidate = parent.create_candidate("owner-state.tmp", private=False)
    candidate.write_all(payload)
    candidate.flush_content()
    pending = parent.begin_publish(
        candidate,
        "owner-state.json",
        mode=PublishMode.REPLACE_UNDER_LOCK,
        lease=lease,
    )
    try:
        if pending.retained_destination().read_all() != payload:
            raise RuntimeError("owner state retained readback mismatch")
        if pending.terminal_reproof() != pending.preliminary_facts():
            raise RuntimeError("owner state terminal reproof mismatch")
    finally:
        pending.close()


def publish(
    root_path: Path,
    *,
    fault_phase: str | None = None,
    fault_kind: str | None = None,
) -> None:
    def platform_fault(phase: str) -> None:
        _trigger(fault_phase, phase, fault_kind)

    adapter = WindowsPlatformAdapter(
        _fault_injector=platform_fault if fault_phase else None
    )
    root = adapter.bind_root(root_path)
    parent = adapter.bind_parent(root, PureWindowsPath("canonical.bin"))
    lease = adapter.acquire(
        parent,
        "documented-publish.lock",
        LOCK_PAYLOAD,
        LockPolicy(LockWait.BLOCK),
    )
    candidate = None
    pending = None
    try:
        candidate = parent.create_candidate("candidate.tmp", private=False)
        _trigger(fault_phase, "candidate_after_create", fault_kind)
        candidate.write_all(NEW_PAYLOAD)
        _trigger(fault_phase, "candidate_after_write", fault_kind)
        candidate.flush_content()
        _trigger(fault_phase, "candidate_after_flush", fault_kind)
        pending = parent.begin_publish(
            candidate,
            "canonical.bin",
            mode=PublishMode.REPLACE_UNDER_LOCK,
            lease=lease,
        )
        candidate = None
        if pending.retained_destination().read_all() != NEW_PAYLOAD:
            raise RuntimeError("canonical retained readback mismatch")
        _trigger(fault_phase, "owner_before_commit", fault_kind)
        _publish_state(parent, lease, _owner_state(NEW_PAYLOAD, "new"))
        _trigger(fault_phase, "owner_after_commit", fault_kind)
        if pending.terminal_reproof() != pending.preliminary_facts():
            raise RuntimeError("canonical terminal reproof mismatch")
        _trigger(fault_phase, "owner_after_terminal", fault_kind)
    finally:
        if pending is not None:
            pending.close()
        if candidate is not None:
            candidate.close()
        lease.close()
        parent.close()
        root.close()


def classify(root_path: Path) -> str:
    adapter = WindowsPlatformAdapter()
    root = adapter.bind_root(root_path)
    canonical = None
    owner_state = None
    try:
        canonical = adapter.open_regular(root, PureWindowsPath("canonical.bin"))
        owner_state = adapter.open_regular(root, PureWindowsPath("owner-state.json"))
        payload = canonical.read_all()
        try:
            state = json.loads(owner_state.read_all().decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return "RECOVERY_REQUIRED"
        if type(state) is not dict or set(state) != {
            "content_sha256",
            "generation",
            "phase",
            "schema",
        }:
            return "RECOVERY_REQUIRED"
        if state["schema"] != SCHEMA or state["phase"] != "COMMITTED":
            return "RECOVERY_REQUIRED"
        if state["content_sha256"] != hashlib.sha256(payload).hexdigest():
            return "RECOVERY_REQUIRED"
        if payload == OLD_PAYLOAD and state["generation"] == "old":
            return "OLD"
        if payload == NEW_PAYLOAD and state["generation"] == "new":
            return "NEW"
        return "RECOVERY_REQUIRED"
    finally:
        if owner_state is not None:
            owner_state.close()
        if canonical is not None:
            canonical.close()
        root.close()


def _child_command(root_path: Path, phase: str | None, fault_kind: str | None) -> list[str]:
    command = [sys.executable, str(Path(__file__).resolve()), "child-publish", str(root_path)]
    if phase is not None:
        command.extend(("--fault-phase", phase, "--fault-kind", str(fault_kind)))
    return command


def run_matrix(root_path: Path, fault_kind: str) -> dict[str, object]:
    results: dict[str, str] = {}
    for phase in FAULT_PHASES:
        phase_root = root_path / phase
        seed(phase_root)
        completed = subprocess.run(
            _child_command(phase_root, phase, fault_kind),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        expected_exit = 87 if fault_kind == "terminate" else 1
        if completed.returncode != expected_exit:
            raise RuntimeError(f"unexpected child exit at {phase}: {completed.returncode}")
        recovery = classify(phase_root)
        if recovery != EXPECTED_RECOVERY[phase]:
            raise RuntimeError(
                f"unexpected recovery at {phase}: {recovery} != {EXPECTED_RECOVERY[phase]}"
            )
        results[phase] = recovery
    return {"fault_kind": fault_kind, "results": results, "schema": SCHEMA}


def run_success(root_path: Path) -> dict[str, object]:
    seed(root_path)
    completed = subprocess.run(
        _child_command(root_path, None, None),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"publish child failed: {completed.returncode}")
    recovery = classify(root_path)
    if recovery != "NEW":
        raise RuntimeError(f"success recovery mismatch: {recovery}")
    return {"recovery": recovery, "schema": SCHEMA}


def _ticket_path(root_path: Path) -> Path:
    return root_path / "reboot-ticket.json"


def _worker_sha256() -> str:
    return hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()


def prepare_reboot(root_path: Path, boot_session: str) -> dict[str, object]:
    outcome = run_success(root_path)
    source_snapshot = source_snapshot_sha256(ROOT)
    ticket = {
        "boot_session": boot_session,
        "canonical_sha256": hashlib.sha256(NEW_PAYLOAD).hexdigest(),
        "schema": TICKET_SCHEMA,
        "source_snapshot_sha256": source_snapshot,
        "state": outcome["recovery"],
        "worker_sha256": _worker_sha256(),
    }
    _write_seed(_ticket_path(root_path), _canonical_json(ticket))
    return {
        "recovery": "NEW",
        "reboot": "PREPARED",
        "schema": TICKET_SCHEMA,
        "source_snapshot_sha256": source_snapshot,
    }


def resume_reboot(root_path: Path, boot_session: str) -> dict[str, object]:
    ticket_path = _ticket_path(root_path)
    try:
        ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("reboot ticket is unavailable") from error
    if type(ticket) is not dict or set(ticket) != {
        "boot_session",
        "canonical_sha256",
        "schema",
        "source_snapshot_sha256",
        "state",
        "worker_sha256",
    }:
        raise RuntimeError("reboot ticket shape mismatch")
    source_snapshot = source_snapshot_sha256(ROOT)
    if (
        ticket["schema"] != TICKET_SCHEMA
        or ticket["canonical_sha256"] != hashlib.sha256(NEW_PAYLOAD).hexdigest()
        or ticket["source_snapshot_sha256"] != source_snapshot
        or ticket["state"] != "NEW"
        or ticket["worker_sha256"] != _worker_sha256()
    ):
        raise RuntimeError("reboot ticket authority mismatch")
    if ticket["boot_session"] == boot_session:
        return {
            "recovery": "NOT_OBSERVED",
            "reboot": "NOT_RUN",
            "schema": TICKET_SCHEMA,
            "source_snapshot_sha256": source_snapshot,
        }
    recovery = classify(root_path)
    if recovery != "NEW":
        raise RuntimeError(f"post-reboot recovery mismatch: {recovery}")
    return {
        "recovery": recovery,
        "reboot": "PASS",
        "schema": TICKET_SCHEMA,
        "source_snapshot_sha256": source_snapshot,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)

    child = subparsers.add_parser("child-publish")
    child.add_argument("root", type=Path)
    child.add_argument("--fault-phase", choices=FAULT_PHASES)
    child.add_argument("--fault-kind", choices=("instruction", "terminate"))

    for action in ("success", "instruction-matrix", "termination-matrix"):
        command = subparsers.add_parser(action)
        command.add_argument("root", type=Path)

    for action in ("reboot-prepare", "reboot-resume"):
        command = subparsers.add_parser(action)
        command.add_argument("root", type=Path)
        command.add_argument("--boot-session", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("WINDOWS_DOCUMENTED_PUBLISH_UNAVAILABLE", file=sys.stderr)
        return 2
    if args.action == "child-publish":
        publish(args.root, fault_phase=args.fault_phase, fault_kind=args.fault_kind)
        return 0
    if args.action == "success":
        result = run_success(args.root)
    elif args.action == "instruction-matrix":
        result = run_matrix(args.root, "instruction")
    elif args.action == "termination-matrix":
        result = run_matrix(args.root, "terminate")
    elif args.action == "reboot-prepare":
        result = prepare_reboot(args.root, args.boot_session)
    else:
        result = resume_reboot(args.root, args.boot_session)
    print(_canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
