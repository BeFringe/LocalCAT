"""Run and externally anchor one tracked Task 1.2 evidence smoke lane.

The producer can prove only bundle-internal consistency.  This orchestrator first
proves that its inputs are the clean, tracked bytes of HEAD, invokes the producer,
and then computes the expected manifest digest outside the producer before the
strict external-anchor validation pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from tools.run_windows_evidence_harness_smoke import LANES
from tools.validate_windows_release import EvidenceValidationError, validate_evidence_bundle


_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")
_ANCHOR_SCHEMA = "localcat.windows-release-external-anchor.v1"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _git(repository_root: Path, *arguments: str, text: bool = True) -> str | bytes:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise EvidenceValidationError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _tracked_head_bytes(repository_root: Path, key: str) -> bytes:
    _git(repository_root, "ls-files", "--error-unmatch", "--", key)
    value = _git(repository_root, "show", "--no-textconv", f"HEAD:{key}", text=False)
    if not isinstance(value, bytes):
        raise AssertionError("binary git output was unexpectedly decoded")
    return value


def _clean_head_context(repository_root: Path, contract_key: str) -> tuple[str, str, str]:
    root_text = _git(repository_root, "rev-parse", "--show-toplevel")
    if not isinstance(root_text, str):
        raise AssertionError("text git output was unexpectedly binary")
    actual_root = Path(root_text.strip()).resolve()
    if actual_root != repository_root.resolve():
        raise EvidenceValidationError("repository root does not match git top level")

    status = _git(
        repository_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if not isinstance(status, str):
        raise AssertionError("text git output was unexpectedly binary")
    if status:
        raise EvidenceValidationError("tracked orchestrator inputs are not clean")

    commit_text = _git(repository_root, "rev-parse", "HEAD^{commit}")
    branch_text = _git(repository_root, "branch", "--show-current")
    if not isinstance(commit_text, str) or not isinstance(branch_text, str):
        raise AssertionError("text git output was unexpectedly binary")
    commit = commit_text.strip()
    branch = branch_text.strip()
    if _COMMIT_RE.fullmatch(commit) is None:
        raise EvidenceValidationError("HEAD is not a lowercase 40-hex commit")
    if _SAFE_BRANCH_RE.fullmatch(branch) is None:
        raise EvidenceValidationError("a named safe branch is required")

    for key in (
        contract_key,
        "tools/run_windows_evidence_harness_smoke.py",
        "tools/anchor_windows_evidence_harness_smoke.py",
        "tools/validate_windows_release.py",
    ):
        head_bytes = _tracked_head_bytes(repository_root, key)
        worktree_bytes = repository_root.joinpath(*key.split("/")).read_bytes()
        if worktree_bytes != head_bytes:
            raise EvidenceValidationError(f"worktree bytes differ from HEAD for {key}")

    return commit, branch, _sha256_bytes(_tracked_head_bytes(repository_root, contract_key))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run and externally anchor one clean tracked evidence smoke lane."
    )
    parser.add_argument("lane", choices=sorted(LANES))
    parser.add_argument("--repository-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--anchor-output", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.name != "nt":
        print("WINDOWS_EVIDENCE_ANCHOR_UNAVAILABLE: Windows is required", file=sys.stderr)
        return 2
    if args.artifact_root.exists() or args.anchor_output.exists():
        print("WINDOWS_EVIDENCE_ANCHOR_INPUT_ERROR: outputs already exist", file=sys.stderr)
        return 2
    try:
        args.anchor_output.resolve().relative_to(args.artifact_root.resolve())
    except ValueError:
        pass
    else:
        print(
            "WINDOWS_EVIDENCE_ANCHOR_INPUT_ERROR: anchor must be outside artifact root",
            file=sys.stderr,
        )
        return 2

    lane = LANES[args.lane]
    contract_key = str(lane["contract"])
    try:
        commit, branch, expected_contract_sha256 = _clean_head_context(
            args.repository_root, contract_key
        )
        producer = args.repository_root / "tools/run_windows_evidence_harness_smoke.py"
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                str(producer),
                args.lane,
                "--repository-root",
                str(args.repository_root),
                "--artifact-root",
                str(args.artifact_root),
                "--repository-commit",
                commit,
                "--repository-branch",
                branch,
                "--run-id",
                args.run_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise EvidenceValidationError(
                f"evidence producer failed with exit {completed.returncode}: "
                f"{completed.stderr.strip()}"
            )
        producer_result = json.loads(completed.stdout)
        if producer_result.get("validation_scope") != "INTERNAL_CONSISTENCY_ONLY":
            raise EvidenceValidationError("producer claimed an external validation scope")
        if producer_result.get("contract_sha256") != expected_contract_sha256:
            raise EvidenceValidationError("producer contract digest differs from tracked HEAD")

        manifest_path = args.artifact_root / "manifest.json"
        expected_manifest_sha256 = _sha256_file(manifest_path)
        if producer_result.get("manifest_sha256") != expected_manifest_sha256:
            raise EvidenceValidationError("producer manifest digest differs from external digest")
        repeated_context = _clean_head_context(args.repository_root, contract_key)
        if repeated_context != (commit, branch, expected_contract_sha256):
            raise EvidenceValidationError("clean tracked HEAD changed during evidence production")
        manifest = validate_evidence_bundle(
            args.artifact_root,
            repository_root=args.repository_root,
            scenario_contract_key=contract_key,
            expected_repository_commit=commit,
            expected_scenario_contract_sha256=expected_contract_sha256,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        anchor = {
            "artifact_key": manifest["run"]["artifact_key"],
            "manifest_sha256": expected_manifest_sha256,
            "repository_branch": branch,
            "repository_commit": commit,
            "run_status": manifest["run"]["status"],
            "scenario_contract_key": contract_key,
            "scenario_contract_sha256": expected_contract_sha256,
            "schema": _ANCHOR_SCHEMA,
            "validation_scope": "EXTERNALLY_ANCHORED",
        }
        args.anchor_output.parent.mkdir(parents=True, exist_ok=True)
        args.anchor_output.write_text(
            json.dumps(anchor, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
    except (EvidenceValidationError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"WINDOWS_EVIDENCE_ANCHOR_FAILED: {error}", file=sys.stderr)
        return 1

    print(json.dumps(anchor, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
