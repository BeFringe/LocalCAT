from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from deterministic_workload import CONTRACT_FILE

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ARTIFACT_DIR = BASE_DIR.parent / "artifacts" / "perf"
DEFAULT_REQUIRED_GROUPS = (5, 50, 200, 800)


@dataclass(frozen=True)
class Thresholds:
    p95_growth_ratio_max: float
    median_growth_ratio_max: float
    throughput_drop_ratio_min: float
    throughput_growth_fraction_min: float


@dataclass(frozen=True)
class GroupSummary:
    group: int
    repeats: int
    init_ms_median: float
    total_ms_median: float
    per_row_us_median: float
    per_row_us_p95: float
    throughput_rows_s: float


def _load_json(path: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _number(row: dict[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"invalid-metric: key={key} value={value!r}")


def _summarize_mode(results: list[dict[str, object]], groups: tuple[int, ...], mode: str) -> list[GroupSummary]:
    summaries: list[GroupSummary] = []
    for group in groups:
        rows = [entry for entry in results if entry.get("mode") == mode and entry.get("group") == group]
        if not rows:
            raise ValueError(f"missing-group: mode={mode} group={group}")

        init_values = [_number(row, "init_ms") for row in rows]
        total_values = [_number(row, "total_ms") for row in rows]
        median_values = [_number(row, "per_row_us_median") for row in rows]
        p95_values = [_number(row, "per_row_us_p95") for row in rows]
        throughput_values = [_number(row, "throughput_rows_s") for row in rows]

        summaries.append(
            GroupSummary(
                group=group,
                repeats=len(rows),
                init_ms_median=round(statistics.median(init_values), 3),
                total_ms_median=round(statistics.median(total_values), 3),
                per_row_us_median=round(statistics.median(median_values), 3),
                per_row_us_p95=round(statistics.median(p95_values), 3),
                throughput_rows_s=round(statistics.median(throughput_values), 3),
            )
        )
    return summaries


def _linearity_checks(summaries: list[GroupSummary], thresholds: Thresholds, mode: str) -> dict[str, object]:
    pairs: list[dict[str, object]] = []
    p95_ratios: list[float] = []
    median_ratios: list[float] = []
    throughput_row_efficiencies: list[float] = []
    throughput_growth_fractions: list[float] = []

    for index in range(1, len(summaries)):
        previous = summaries[index - 1]
        current = summaries[index]
        size_ratio = current.group / previous.group

        p95_ratio = _safe_ratio(current.per_row_us_p95, previous.per_row_us_p95)
        median_ratio = _safe_ratio(current.per_row_us_median, previous.per_row_us_median)
        throughput_ratio = _safe_ratio(current.throughput_rows_s, previous.throughput_rows_s)
        throughput_row_efficiency = _safe_ratio(throughput_ratio, size_ratio)

        p95_ratios.append(p95_ratio)
        median_ratios.append(median_ratio)
        exclude_from_throughput_gate = mode == "warm" and previous.group == 5
        if not exclude_from_throughput_gate:
            throughput_row_efficiencies.append(throughput_row_efficiency)
            throughput_growth_fractions.append(throughput_ratio)

        pairs.append(
            {
                "pair": f"{previous.group}->{current.group}",
                "size_ratio": round(size_ratio, 3),
                "p95_growth_ratio": round(p95_ratio, 3),
                "median_growth_ratio": round(median_ratio, 3),
                "throughput_growth_ratio": round(throughput_ratio, 3),
                "throughput_efficiency_vs_size": round(throughput_row_efficiency, 3),
            }
        )

    p95_max = max(p95_ratios) if p95_ratios else 0.0
    median_max = max(median_ratios) if median_ratios else 0.0
    throughput_efficiency_min = min(throughput_row_efficiencies) if throughput_row_efficiencies else 0.0
    throughput_growth_min = min(throughput_growth_fractions) if throughput_growth_fractions else 0.0
    pass_value = (
        p95_max <= thresholds.p95_growth_ratio_max
        and median_max <= thresholds.median_growth_ratio_max
        and throughput_efficiency_min >= thresholds.throughput_drop_ratio_min
        and throughput_growth_min >= thresholds.throughput_growth_fraction_min
    )

    return {
        "pass": pass_value,
        "pairs": pairs,
        "deltas": {
            "p95_growth_ratio_max": round(p95_max, 3),
            "median_growth_ratio_max": round(median_max, 3),
            "throughput_efficiency_vs_size_min": round(throughput_efficiency_min, 3),
            "throughput_growth_ratio_min": round(throughput_growth_min, 3),
        },
        "thresholds": {
            "p95_growth_ratio_max": thresholds.p95_growth_ratio_max,
            "median_growth_ratio_max": thresholds.median_growth_ratio_max,
            "throughput_efficiency_vs_size_min": thresholds.throughput_drop_ratio_min,
            "throughput_growth_ratio_min": thresholds.throughput_growth_fraction_min,
        },
    }


def _build_markdown(mode_summaries: dict[str, list[GroupSummary]], mode_checks: dict[str, dict[str, object]]) -> str:
    lines = ["# Backend Scaling Linearity Gate", ""]
    for mode, summaries in mode_summaries.items():
        lines.append(f"## Mode: {mode}")
        lines.append("group | repeats | median_us | p95_us | throughput_rows_s")
        lines.append("--- | --- | --- | --- | ---")
        for row in summaries:
            lines.append(
                f"{row.group} | {row.repeats} | {row.per_row_us_median:.3f} | {row.per_row_us_p95:.3f} | {row.throughput_rows_s:.3f}"
            )
        mode_pass = cast(bool, mode_checks[mode]["pass"])
        lines.append("")
        lines.append(f"Gate pass: {'YES' if mode_pass else 'NO'}")
        lines.append("")
    return "\n".join(lines) + "\n"


def run_gate(
    backend_artifact: Path,
    contract_path: Path,
    report_dir: Path,
    markdown: bool,
) -> tuple[bool, Path, Path | None]:
    artifact = _load_json(backend_artifact)
    contract = _load_json(contract_path)
    benchmark_config = cast(dict[str, object], contract.get("benchmark_config", {}))
    configured_groups = cast(list[object], benchmark_config.get("groups", list(DEFAULT_REQUIRED_GROUPS)))
    groups = tuple(value for value in configured_groups if isinstance(value, int))
    required_groups = groups or DEFAULT_REQUIRED_GROUPS

    results = cast(list[dict[str, object]], artifact.get("results", []))
    if not results:
        raise ValueError("empty-results: backend artifact has no result rows")

    modes = cast(list[object], artifact.get("modes", ["cold", "warm"]))
    selected_modes = tuple(mode for mode in modes if isinstance(mode, str))
    if not selected_modes:
        selected_modes = ("cold", "warm")

    thresholds = Thresholds(
        p95_growth_ratio_max=2.5,
        median_growth_ratio_max=2.0,
        throughput_drop_ratio_min=0.1,
        throughput_growth_fraction_min=0.5,
    )

    mode_summaries: dict[str, list[GroupSummary]] = {}
    mode_checks: dict[str, dict[str, object]] = {}
    for mode in selected_modes:
        summaries = _summarize_mode(results, required_groups, mode)
        checks = _linearity_checks(summaries, thresholds, mode)
        mode_summaries[mode] = summaries
        mode_checks[mode] = checks

    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"backend_scaling_gate_{stamp}.json"

    payload = {
        "gate": "backend_scaling_linearity",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_backend_artifact": str(backend_artifact.resolve()),
        "contract": str(contract_path.resolve()),
        "required_groups": list(required_groups),
        "modes": list(selected_modes),
        "mode_summaries": {
            mode: [
                {
                    "group": row.group,
                    "repeats": row.repeats,
                    "init_ms_median": row.init_ms_median,
                    "total_ms_median": row.total_ms_median,
                    "per_row_us_median": row.per_row_us_median,
                    "per_row_us_p95": row.per_row_us_p95,
                    "throughput_rows_s": row.throughput_rows_s,
                }
                for row in rows
            ]
            for mode, rows in mode_summaries.items()
        },
        "mode_linearity": mode_checks,
        "pass": all(cast(bool, checks["pass"]) for checks in mode_checks.values()),
    }
    _ = report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    markdown_path: Path | None = None
    if markdown:
        markdown_path = report_dir / f"backend_scaling_gate_{stamp}.md"
        _ = markdown_path.write_text(_build_markdown(mode_summaries, mode_checks), encoding="utf-8")

    return cast(bool, payload["pass"]), report_path, markdown_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Backend scaling and linearity decision gate")
    _ = parser.add_argument("--backend-artifact", type=Path, required=True, help="Backend throughput JSON artifact")
    _ = parser.add_argument("--contract", type=Path, default=CONTRACT_FILE, help="Benchmark contract JSON path")
    _ = parser.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help="Directory for scaling report artifacts",
    )
    _ = parser.add_argument("--markdown", action="store_true", help="Also emit markdown summary")
    args = parser.parse_args()

    backend_artifact = cast(Path, args.backend_artifact).expanduser().resolve()
    contract_path = cast(Path, args.contract).expanduser().resolve()
    report_dir = cast(Path, args.report_dir).expanduser().resolve()

    try:
        passed, report_path, markdown_path = run_gate(
            backend_artifact=backend_artifact,
            contract_path=contract_path,
            report_dir=report_dir,
            markdown=cast(bool, args.markdown),
        )
    except (ValueError, KeyError, TypeError) as exc:
        print(f"backend scaling gate failed: {exc}")
        return 2

    print(f"backend scaling report written: {report_path}")
    if markdown_path is not None:
        print(f"backend scaling summary written: {markdown_path}")
    if not passed:
        print("backend scaling gate: FAIL")
        return 1
    print("backend scaling gate: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
