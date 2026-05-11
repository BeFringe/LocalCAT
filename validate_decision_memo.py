#!/usr/bin/env python3
# pyright: basic
"""Validate Task-8 decision gate memo outputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


REQUIRED_MARKDOWN_SECTIONS = [
    "## Decision",
    "## Evidence Snapshot",
    "## Ranked Top 3 Optimization Candidates",
    "## UNO/PyOO Reconsider Triggers",
    "## Next-Wave Plan",
]


def validate_json_schema(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return False, f"missing decision memo json: {path}"

    try:
        with path.open("r", encoding="utf-8") as handle:
            memo: Dict[str, Any] = json.load(handle)
    except json.JSONDecodeError as exc:
        return False, f"invalid decision memo json: {exc}"

    required_top = [
        "memo_type",
        "generated_at",
        "input_report",
        "decision",
        "evidence_snapshot",
        "top_optimization_candidates",
        "uno_pyoo_reconsider_triggers",
        "next_wave_plan",
    ]
    for key in required_top:
        if key not in memo:
            return False, f"missing json key: {key}"

    if memo["memo_type"] != "decision_gate_optimization_backlog":
        return False, "memo_type must be decision_gate_optimization_backlog"

    candidates = memo["top_optimization_candidates"]
    if not isinstance(candidates, list) or len(candidates) != 3:
        return False, "top_optimization_candidates must contain exactly 3 entries"

    expected_ranks = [1, 2, 3]
    actual_ranks: List[int] = [int(c.get("rank", -1)) for c in candidates]
    if actual_ranks != expected_ranks:
        return False, f"candidate ranks must be {expected_ranks}, got {actual_ranks}"

    for candidate in candidates:
        for key in ["title", "rationale", "expected_impact", "risk"]:
            if key not in candidate or not str(candidate[key]).strip():
                return False, f"candidate missing non-empty key: {key}"

    triggers = memo["uno_pyoo_reconsider_triggers"]
    if not isinstance(triggers, list) or len(triggers) < 1:
        return False, "uno_pyoo_reconsider_triggers must contain at least one trigger"

    for trigger in triggers:
        for key in ["id", "condition", "why"]:
            if key not in trigger or not str(trigger[key]).strip():
                return False, f"trigger missing non-empty key: {key}"

    if str(memo["decision"].get("uno_pyoo_now", "")).lower() != "no":
        return False, "decision.uno_pyoo_now must be 'no' for current evidence"

    return True, "decision memo json is valid"


def validate_markdown(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return False, f"missing decision memo markdown: {path}"

    content = path.read_text(encoding="utf-8")
    for section in REQUIRED_MARKDOWN_SECTIONS:
        if section not in content:
            return False, f"missing markdown section: {section}"

    for rank in ["### 1.", "### 2.", "### 3."]:
        if rank not in content:
            return False, f"missing ranked candidate heading: {rank}"

    if "UNO/pyoo now: **no**" not in content:
        return False, "markdown must include explicit no-UNO-now decision"

    return True, "decision memo markdown is valid"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate decision gate memo outputs")
    parser.add_argument(
        "--memo-json",
        default="artifacts/perf/decision_gate_optimization_backlog.json",
        help="Path to decision memo JSON",
    )
    parser.add_argument(
        "--memo-md",
        default="artifacts/perf/decision_gate_optimization_backlog.md",
        help="Path to decision memo markdown",
    )
    args = parser.parse_args()

    json_ok, json_message = validate_json_schema(Path(args.memo_json))
    if not json_ok:
        print(f"validation-error: {json_message}")
        return 1

    md_ok, md_message = validate_markdown(Path(args.memo_md))
    if not md_ok:
        print(f"validation-error: {md_message}")
        return 1

    print(json_message)
    print(md_message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
