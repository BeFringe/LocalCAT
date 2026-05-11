from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from deterministic_workload import CONTRACT_FILE, load_contract
from logic_controller import LogicController

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_WORKLOAD_DIR = BASE_DIR / "workloads" / "deterministic"
DEFAULT_ARTIFACT_DIR = BASE_DIR.parent / "artifacts" / "perf"


@dataclass(frozen=True)
class HarnessRow:
    group: int
    rows: int
    repeat: int
    mode: str
    init_ms: float
    total_ms: float
    per_row_us_median: float
    per_row_us_p95: float
    throughput_rows_s: float


def _percentile(sorted_values: list[float], percentile: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = (len(sorted_values) - 1) * percentile
    low = int(index)
    high = min(low + 1, len(sorted_values) - 1)
    if low == high:
        return sorted_values[low]
    weight = index - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def _load_workload_rows(workload_path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with workload_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            payload = line.strip()
            if not payload:
                continue
            row = cast(dict[str, object], json.loads(payload))
            rows.append(row)
    return rows


def _validate_result_shape(result: dict[str, object], contract: dict[str, object]) -> None:
    runtime = cast(dict[str, object], contract["runtime_schema_source"])
    statuses = cast(list[object], runtime["statuses"])
    payload_keys = cast(dict[str, object], runtime["payload_keys_by_status"])
    status = result.get("status")
    if not isinstance(status, str) or status not in statuses:
        raise RuntimeError(f"Unexpected status from controller: {status!r}")
    per_status = cast(dict[str, object], payload_keys[status])
    top_level = cast(list[object], per_status["required_top_level"])
    for key in top_level:
        if key not in result:
            raise RuntimeError(f"Missing top-level key '{key}' for status {status}")


def _run_repeat(
    rows: list[dict[str, object]],
    mode: str,
    repeat: int,
    contract: dict[str, object],
    warm_controller: LogicController | None,
) -> tuple[HarnessRow, LogicController | None]:
    repeat_start = time.perf_counter_ns()
    controller: LogicController | None = warm_controller
    init_ms = 0.0
    if mode == "cold" or controller is None:
        init_start = time.perf_counter_ns()
        controller = LogicController()
        init_end = time.perf_counter_ns()
        init_ms = (init_end - init_start) / 1_000_000.0

    per_row_us: list[float] = []
    for row in rows:
        source = row.get("source")
        if not isinstance(source, str):
            raise RuntimeError("Workload row missing string 'source'.")
        query_start = time.perf_counter_ns()
        result = controller.get_suggestions(source)
        query_end = time.perf_counter_ns()
        _validate_result_shape(result, contract)
        per_row_us.append((query_end - query_start) / 1_000.0)

    repeat_end = time.perf_counter_ns()
    total_ms = (repeat_end - repeat_start) / 1_000_000.0
    sorted_us = sorted(per_row_us)
    median_us = statistics.median(sorted_us) if sorted_us else 0.0
    p95_us = _percentile(sorted_us, 0.95)
    throughput = (len(rows) / (total_ms / 1000.0)) if total_ms > 0 else 0.0

    group_value = 0
    if rows:
        raw_group = rows[0].get("group")
        if isinstance(raw_group, int):
            group_value = raw_group

    result_row = HarnessRow(
        group=group_value,
        rows=len(rows),
        repeat=repeat,
        mode=mode,
        init_ms=round(init_ms, 3),
        total_ms=round(total_ms, 3),
        per_row_us_median=round(median_us, 3),
        per_row_us_p95=round(p95_us, 3),
        throughput_rows_s=round(throughput, 3),
    )
    return result_row, controller if mode == "warm" else None


def run_harness(
    groups: tuple[int, ...],
    repeats: int,
    warmup: int,
    modes: tuple[str, ...],
    workload_dir: Path,
    artifact_dir: Path,
    contract_path: Path,
) -> Path:
    contract = cast(dict[str, object], json.loads(contract_path.read_text(encoding="utf-8")))
    runs: list[dict[str, object]] = []

    for group in groups:
        workload_path = workload_dir / f"workload_{group}.jsonl"
        if not workload_path.exists():
            raise FileNotFoundError(f"Workload file not found: {workload_path}")
        rows = _load_workload_rows(workload_path)
        if not rows:
            raise RuntimeError(f"Workload file is empty: {workload_path}")

        for mode in modes:
            warm_controller: LogicController | None = None
            if mode == "warm":
                warm_controller = LogicController()
                for _ in range(warmup):
                    _, warm_controller = _run_repeat(
                        rows=rows,
                        mode=mode,
                        repeat=0,
                        contract=contract,
                        warm_controller=warm_controller,
                    )
            for repeat in range(1, repeats + 1):
                run_row, warm_controller = _run_repeat(
                    rows=rows,
                    mode=mode,
                    repeat=repeat,
                    contract=contract,
                    warm_controller=warm_controller,
                )
                runs.append(
                    {
                        "group": run_row.group,
                        "rows": run_row.rows,
                        "repeat": run_row.repeat,
                        "mode": run_row.mode,
                        "init_ms": run_row.init_ms,
                        "total_ms": run_row.total_ms,
                        "per_row_us_median": run_row.per_row_us_median,
                        "per_row_us_p95": run_row.per_row_us_p95,
                        "throughput_rows_s": run_row.throughput_rows_s,
                    }
                )

    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = artifact_dir / f"backend_throughput_{stamp}.json"
    payload = {
        "harness": "backend_only_logic_controller",
        "backend_path": "LogicController.__init__ + LogicController.get_suggestions",
        "spreadsheet_io": "excluded",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "groups": list(groups),
        "repeats": repeats,
        "modes": list(modes),
        "results": runs,
    }
    _ = output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output_path


def main() -> int:
    contract = load_contract(CONTRACT_FILE)
    parser = argparse.ArgumentParser(description="Backend-only throughput benchmark harness")
    _ = parser.add_argument(
        "--groups",
        type=lambda value: tuple(int(x.strip()) for x in value.split(",") if x.strip()),
        default=contract.groups,
        help="Comma-separated workload groups (defaults to contract groups)",
    )
    _ = parser.add_argument(
        "--repeats",
        type=int,
        default=None,
        help="Repeat count per group and mode",
    )
    _ = parser.add_argument(
        "--warmup",
        type=int,
        default=None,
        help="Warmup passes before warm-mode timed repeats",
    )
    _ = parser.add_argument(
        "--modes",
        choices=["cold", "warm", "both"],
        default="both",
        help="Run cold, warm, or both modes",
    )
    _ = parser.add_argument(
        "--workload-dir",
        type=Path,
        default=DEFAULT_WORKLOAD_DIR,
        help="Directory containing deterministic workload jsonl files",
    )
    _ = parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_DIR,
        help="Directory where JSON benchmark artifact is written",
    )
    _ = parser.add_argument(
        "--contract",
        type=Path,
        default=CONTRACT_FILE,
        help="Benchmark contract JSON path",
    )
    args = parser.parse_args()

    groups = cast(tuple[int, ...], args.groups)
    workload_dir = cast(Path, args.workload_dir).expanduser().resolve()
    artifact_dir = cast(Path, args.artifact_dir).expanduser().resolve()
    contract_path = cast(Path, args.contract).expanduser().resolve()
    modes_flag = cast(str, args.modes)
    modes = ("cold", "warm") if modes_flag == "both" else (modes_flag,)
    benchmark_contract = cast(dict[str, object], json.loads(contract_path.read_text(encoding="utf-8")))
    benchmark_config = cast(dict[str, object], benchmark_contract.get("benchmark_config", {}))
    default_repeats = benchmark_config.get("repeats", 5)
    if not isinstance(default_repeats, int):
        default_repeats = 5
    default_warmup = benchmark_config.get("warmup", 1)
    if not isinstance(default_warmup, int):
        default_warmup = 1

    repeats_raw = cast(int | None, args.repeats)
    warmup_raw = cast(int | None, args.warmup)
    repeats = repeats_raw if repeats_raw is not None else default_repeats
    warmup = warmup_raw if warmup_raw is not None else default_warmup

    output_path = run_harness(
        groups=groups,
        repeats=repeats,
        warmup=warmup,
        modes=modes,
        workload_dir=workload_dir,
        artifact_dir=artifact_dir,
        contract_path=contract_path,
    )
    print(f"backend throughput artifact written: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
