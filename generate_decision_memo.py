#!/usr/bin/env python3
# pyright: basic
"""Generate Task-8 decision gate memo from comparative benchmark evidence."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


REQUIRED_GROUPS = ["5_rows", "50_rows", "200_rows", "800_rows"]
FOCUS_GROUPS = ["50_rows", "200_rows", "800_rows"]


@dataclass(frozen=True)
class Candidate:
    rank: int
    title: str
    rationale: str
    expected_impact: str
    risk: str


def _fail(message: str) -> int:
    print(f"dependency-missing-error: {message}")
    return 1


def _load_report(report_path: Path) -> Tuple[bool, Dict[str, Any] | str]:
    if not report_path.exists():
        return False, f"required input report not found: {report_path}"

    try:
        with report_path.open("r", encoding="utf-8") as handle:
            report = json.load(handle)
    except json.JSONDecodeError as exc:
        return False, f"invalid JSON in report {report_path}: {exc}"

    if report.get("report_type") != "comparative_bottleneck_report":
        return False, "input report_type must be comparative_bottleneck_report"

    group_map = report.get("per_group_comparison")
    if not isinstance(group_map, dict):
        return False, "input report missing per_group_comparison"

    for key in REQUIRED_GROUPS:
        if key not in group_map:
            return False, f"input report missing required group: {key}"

    return True, report


def _segment_shares(report: Dict[str, Any]) -> Dict[str, float]:
    totals = {
        "load_xlsx": 0.0,
        "init_engines": 0.0,
        "compute_rows": 0.0,
        "write_cells": 0.0,
        "save_xlsx": 0.0,
    }

    for group in FOCUS_GROUPS:
        timings = report["per_group_comparison"][group]["openpyxl"]["timings_ms"]
        total = float(sum(timings.values()))
        for segment in totals:
            totals[segment] += float(timings[segment]) / total

    return {segment: value / len(FOCUS_GROUPS) for segment, value in totals.items()}


def _build_candidates(report: Dict[str, Any]) -> Tuple[List[Candidate], Dict[str, float]]:
    shares = _segment_shares(report)

    row_800 = report["per_group_comparison"]["800_rows"]
    openpyxl_800_total = float(row_800["openpyxl"]["total_ms"])
    backend_warm_800 = float(row_800["backend"]["warm"]["total_ms"])
    backend_cold_800 = float(row_800["backend"]["cold"]["init_ms"])

    candidates = [
        Candidate(
            rank=1,
            title="Optimize workbook save path for batch writes",
            rationale=(
                "At strategic sizes (50/200/800), save_xlsx contributes "
                f"~{shares['save_xlsx'] * 100:.1f}% of openpyxl wall time on average and is "
                "the dominant 800-row segment."
            ),
            expected_impact=(
                "Expected medium-high impact: reducing save_xlsx by 30-50% would lower "
                f"800-row batch time by roughly {openpyxl_800_total * 0.30:.1f}-"
                f"{openpyxl_800_total * 0.50:.1f} ms."
            ),
            risk="Medium: workbook fidelity/regression risk if write mode or save strategy changes.",
        ),
        Candidate(
            rank=2,
            title="Amortize openpyxl engine initialization across batches",
            rationale=(
                "init_engines is still ~"
                f"{shares['init_engines'] * 100:.1f}% average share over 50/200/800 and dominates "
                "the 50-row case."
            ),
            expected_impact=(
                "Expected medium impact: persistent engine/session reuse can remove repetitive "
                "startup cost on repeated runs, with strongest gains for 50-row and mixed-size workloads."
            ),
            risk="Medium: lifecycle management complexity and stale-state edge cases.",
        ),
        Candidate(
            rank=3,
            title="Reduce backend cold-start overhead for first batch",
            rationale=(
                "Backend compute is already sub-millisecond per row warm, but cold init remains the "
                f"largest backend first-run cost (~{backend_cold_800:.1f} ms at 800 rows)."
            ),
            expected_impact=(
                "Expected low-medium impact on total openpyxl pipeline, but meaningful UX improvement "
                "for first-request latency and CLI startup responsiveness."
            ),
            risk="Low: bounded scope (prefetch/warmup/cache policy) with minimal output-semantic risk.",
        ),
    ]

    del backend_warm_800
    return candidates, shares


def _build_json_memo(report: Dict[str, Any], report_path: Path) -> Dict[str, Any]:
    candidates, shares = _build_candidates(report)
    row_200 = report["per_group_comparison"]["200_rows"]
    row_800 = report["per_group_comparison"]["800_rows"]
    backend_warm_200 = float(row_200["backend"]["warm"]["total_ms"])
    backend_warm_800 = float(row_800["backend"]["warm"]["total_ms"])
    openpyxl_200 = float(row_200["openpyxl"]["total_ms"])
    openpyxl_800 = float(row_800["openpyxl"]["total_ms"])

    return {
        "memo_type": "decision_gate_optimization_backlog",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "input_report": str(report_path),
        "decision": {
            "uno_pyoo_now": "no",
            "rationale": (
                "Current bottlenecks are primarily openpyxl load/save and engine init costs, while backend "
                "warm compute is already far below spreadsheet I/O cost. Prioritize local, lower-risk "
                "optimizations before introducing UNO/pyoo integration complexity."
            ),
        },
        "evidence_snapshot": {
            "openpyxl_total_ms": {
                "200_rows": openpyxl_200,
                "800_rows": openpyxl_800,
            },
            "backend_warm_total_ms": {
                "200_rows": backend_warm_200,
                "800_rows": backend_warm_800,
            },
            "average_openpyxl_segment_share_50_200_800": {
                key: round(value, 4) for key, value in shares.items()
            },
        },
        "top_optimization_candidates": [candidate.__dict__ for candidate in candidates],
        "uno_pyoo_reconsider_triggers": [
            {
                "id": "trigger_latency_800",
                "condition": "After top-3 backlog work is shipped, 800-row openpyxl median total_ms remains > 120 ms across >= 3 benchmark reruns",
                "why": "Indicates local file-mode optimizations are insufficient for next-wave SLA targets.",
            },
            {
                "id": "trigger_io_dominance",
                "condition": "After top-3 backlog work, load_xlsx + save_xlsx still account for >= 80% of 800-row total_ms",
                "why": "Shows persistent file I/O ceiling that may justify alternate integration architecture.",
            },
            {
                "id": "trigger_capability_gap",
                "condition": "Product requirements add mandatory live spreadsheet interoperability features not achievable via openpyxl file mode",
                "why": "Capability mismatch, not raw throughput, becomes primary decision driver.",
            },
        ],
        "next_wave_plan": [
            "Implement candidate #1 (save path) and rerun full 5/50/200/800 matrix.",
            "Implement candidate #2 (init amortization) and rerun matrix with repeated invocations.",
            "Implement candidate #3 (cold-start reduction), then run end-to-end decision gate again.",
            "Apply UNO/pyoo only if any explicit trigger condition is met.",
        ],
    }


def _build_markdown_memo(memo: Dict[str, Any]) -> str:
    cands = memo["top_optimization_candidates"]
    triggers = memo["uno_pyoo_reconsider_triggers"]
    shares = memo["evidence_snapshot"]["average_openpyxl_segment_share_50_200_800"]

    lines = [
        "# Decision Gate: Optimization Backlog and Next-Wave Plan",
        "",
        f"Generated: {memo['generated_at']}",
        f"Input report: {memo['input_report']}",
        "",
        "## Decision",
        f"- UNO/pyoo now: **{memo['decision']['uno_pyoo_now']}**",
        f"- Rationale: {memo['decision']['rationale']}",
        "",
        "## Evidence Snapshot",
        "- 200 rows: openpyxl total {0:.3f} ms vs backend warm {1:.3f} ms".format(
            memo["evidence_snapshot"]["openpyxl_total_ms"]["200_rows"],
            memo["evidence_snapshot"]["backend_warm_total_ms"]["200_rows"],
        ),
        "- 800 rows: openpyxl total {0:.3f} ms vs backend warm {1:.3f} ms".format(
            memo["evidence_snapshot"]["openpyxl_total_ms"]["800_rows"],
            memo["evidence_snapshot"]["backend_warm_total_ms"]["800_rows"],
        ),
        "- Avg openpyxl segment share (50/200/800): "
        f"load={shares['load_xlsx'] * 100:.1f}%, init={shares['init_engines'] * 100:.1f}%, "
        f"compute={shares['compute_rows'] * 100:.1f}%, write={shares['write_cells'] * 100:.1f}%, "
        f"save={shares['save_xlsx'] * 100:.1f}%",
        "",
        "## Ranked Top 3 Optimization Candidates",
    ]

    for cand in cands:
        lines.extend(
            [
                f"### {cand['rank']}. {cand['title']}",
                f"- Rationale: {cand['rationale']}",
                f"- Expected impact: {cand['expected_impact']}",
                f"- Risk: {cand['risk']}",
                "",
            ]
        )

    lines.append("## UNO/PyOO Reconsider Triggers")
    for trigger in triggers:
        lines.extend(
            [
                f"### {trigger['id']}",
                f"- Condition: {trigger['condition']}",
                f"- Why: {trigger['why']}",
                "",
            ]
        )

    lines.append("## Next-Wave Plan")
    for item in memo["next_wave_plan"]:
        lines.append(f"- {item}")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate optimization decision gate memo from comparative report")
    parser.add_argument(
        "--comparative-report",
        default="artifacts/perf/comparative_bottleneck_report.json",
        help="Path to comparative bottleneck report JSON",
    )
    parser.add_argument(
        "--output-json",
        default="artifacts/perf/decision_gate_optimization_backlog.json",
        help="Path to output decision memo JSON",
    )
    parser.add_argument(
        "--output-md",
        default="artifacts/perf/decision_gate_optimization_backlog.md",
        help="Path to output decision memo markdown",
    )
    args = parser.parse_args()

    report_path = Path(args.comparative_report)
    ok, report_or_error = _load_report(report_path)
    if not ok:
        return _fail(str(report_or_error))

    report = report_or_error
    if not isinstance(report, dict):
        return _fail("unexpected report payload type")

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)

    memo = _build_json_memo(report, report_path)
    markdown = _build_markdown_memo(memo)

    with output_json.open("w", encoding="utf-8") as handle:
        json.dump(memo, handle, indent=2)

    with output_md.open("w", encoding="utf-8") as handle:
        handle.write(markdown)

    print(f"generated memo JSON: {output_json}")
    print(f"generated memo Markdown: {output_md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
