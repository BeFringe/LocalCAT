# pyright: reportGeneralTypeIssues=false, reportUnknownVariableType=false, reportUnknownMemberType=false
"""Deterministic workload generator and validator for benchmark groups."""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable, Sequence
from typing import cast

BASE_DIR = Path(__file__).resolve().parent
CONTRACT_FILE = BASE_DIR / "benchmark_contract.json"
DEFAULT_TM_FILE = BASE_DIR / "tm.jsonl"
DEFAULT_TERMS_FILE = BASE_DIR / "terms.csv"
DEFAULT_PO_FILE = BASE_DIR / "po/卷一_引.json"
DEFAULT_OUTPUT_DIR = BASE_DIR / "workloads" / "deterministic"
DEFAULT_TOLERANCE = 5.0


@dataclass(frozen=True)
class WorkloadContract:
    groups: tuple[int, ...]
    status_mix: dict[str, int]
    statuses: tuple[str, ...]


@dataclass(frozen=True)
class WorkloadSources:
    tm_records: Sequence[dict[str, str]]
    terms: Sequence[dict[str, str]]
    sentences: Sequence[str]
    glossary_source: str


def load_contract(contract_path: Path | None = None) -> WorkloadContract:
    path = (contract_path or CONTRACT_FILE).expanduser().resolve()
    raw = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    runtime = cast(dict[str, object], raw.get("runtime_schema_source", {}))
    config = cast(dict[str, object], raw.get("benchmark_config", {}))
    statuses_candidates = cast(
        Sequence[object],
        runtime.get("statuses", ("TM_HIT", "TERMS_FOUND", "NO_MATCH")),
    )
    extracted_statuses: list[str] = []
    for value in statuses_candidates:
        if isinstance(value, str):
            extracted_statuses.append(value)
    statuses = tuple(extracted_statuses) or ("TM_HIT", "TERMS_FOUND", "NO_MATCH")
    mix_source = cast(dict[str, object], config.get("status_mix", {}))
    mix: dict[str, int] = {}
    for status in statuses:
        raw_value = mix_source.get(status, 0)
        if isinstance(raw_value, int):
            mix[status] = raw_value
        elif isinstance(raw_value, str):
            try:
                mix[status] = int(raw_value)
            except ValueError:
                mix[status] = 0
        else:
            mix[status] = 0
    groups_source = cast(Sequence[object], config.get("groups", (5, 50, 200, 800)))
    groups_list: list[int] = [value for value in groups_source if isinstance(value, int)]
    groups = tuple(groups_list or (5, 50, 200, 800))
    return WorkloadContract(groups=groups, status_mix=mix, statuses=statuses)


def load_tm_records(path: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    resolved = path.expanduser().resolve()
    with resolved.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(cast(dict[str, str], json.loads(line)))
    return records


def load_terms(path: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    resolved = path.expanduser().resolve()
    with resolved.open("r", encoding="utf-8") as handle:
        _ = next(handle, None)
        for line in handle:
            cols = [cell.strip() for cell in line.strip().split(",")]
            if len(cols) < 2 or not cols[0] or not cols[1]:
                continue
            entries.append({"source": cols[0], "target": cols[1]})
    return entries


def load_sentences(path: Path) -> list[str]:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        return []
    payload_obj = cast(object, json.loads(resolved.read_text(encoding="utf-8")))
    if not isinstance(payload_obj, list):
        return []
    payload = cast(Sequence[object], payload_obj)
    sentences: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        entry_dict = cast(dict[str, object], entry)
        source = entry_dict.get("source")
        if isinstance(source, str) and source.strip():
            sentences.append(source.strip())
    return sentences


def allocate_counts(total: int, mix: dict[str, int], order: Iterable[str]) -> dict[str, int]:
    fractions = {status: total * mix.get(status, 0) / 100.0 for status in mix}
    counts = {status: int(fractions.get(status, 0)) for status in mix}
    remaining = total - sum(counts.values())
    if remaining <= 0:
        return counts
    fractional_parts = {
        status: fractions.get(status, 0) - counts.get(status, 0)
        for status in mix
    }
    order_index = {status: idx for idx, status in enumerate(order)}
    sorted_statuses = sorted(
        mix,
        key=lambda status: (
            -fractional_parts.get(status, 0),
            order_index.get(status, 0),
        ),
    )
    idx = 0
    while remaining > 0:
        counts[sorted_statuses[idx % len(sorted_statuses)]] += 1
        remaining -= 1
        idx += 1
    return counts


def build_tm_hit_row(
    group: int,
    status: str,
    index: int,
    rng: random.Random,
    sources: WorkloadSources,
) -> dict[str, object]:
    record = rng.choice(sources.tm_records)
    tm_match = {
        "source": record.get("source", ""),
        "target": record.get("target", ""),
        "match_type": "EXACT",
        "similarity": round(rng.random() * 0.1 + 0.9, 3),
    }
    if record.get("speaker"):
        tm_match["speaker"] = record["speaker"]
    if record.get("file_source"):
        tm_match["file_source"] = record["file_source"]
    return {
        "row_id": f"{group:03d}-{status}-{index:04d}",
        "group": group,
        "status": status,
        "source": record.get("source", ""),
        "tm_match": tm_match,
    }


def build_terms_found_row(
    group: int,
    status: str,
    index: int,
    rng: random.Random,
    sources: WorkloadSources,
) -> dict[str, object]:
    if not sources.terms:
        raise RuntimeError("No glossary terms available to build TERMS_FOUND rows.")
    pool_size = min(len(sources.terms), 3)
    num_terms = rng.randint(1, pool_size)
    picks = (
        rng.sample(sources.terms, num_terms)
        if len(sources.terms) >= num_terms
        else [rng.choice(sources.terms) for _ in range(num_terms)]
    )
    source_pieces: list[str] = [term["source"] for term in picks]
    context = rng.choice(sources.sentences) if sources.sentences else None
    source_text = " ".join(source_pieces)
    if context:
        source_text = f"{source_text} — {context}"
    terms: list[dict[str, object]] = []
    cursor = 0
    for term in picks:
        term_text = term["source"]
        length = len(term_text)
        terms.append(
            {
                "source_term": term_text,
                "target_term": term["target"],
                "start_index": cursor,
                "end_index": cursor + length,
                "glossary_source": sources.glossary_source,
            }
        )
        cursor += length + 1
    return {
        "row_id": f"{group:03d}-{status}-{index:04d}",
        "group": group,
        "status": status,
        "source": source_text,
        "terms": terms,
    }


def build_no_match_row(
    group: int,
    status: str,
    index: int,
    rng: random.Random,
    sources: WorkloadSources,
) -> dict[str, object]:
    if sources.sentences:
        source_text = rng.choice(sources.sentences)
    else:
        source_text = "No match sample data."
    return {
        "row_id": f"{group:03d}-{status}-{index:04d}",
        "group": group,
        "status": status,
        "source": source_text,
    }


ROW_BUILDERS = {
    "TM_HIT": build_tm_hit_row,
    "TERMS_FOUND": build_terms_found_row,
    "NO_MATCH": build_no_match_row,
}


def generate_workloads(
    seed: int,
    contract_path: Path | None,
    tm_path: Path,
    terms_path: Path,
    po_path: Path,
    output_dir: Path,
    groups: Sequence[int] | None,
) -> None:
    contract = load_contract(contract_path)
    data = WorkloadSources(
        tm_records=load_tm_records(tm_path),
        terms=load_terms(terms_path),
        sentences=load_sentences(po_path),
        glossary_source=terms_path.name,
    )
    rng = random.Random(seed)
    out_dir = output_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    groups_to_build = tuple(groups) if groups else contract.groups
    for group in groups_to_build:
        counts = allocate_counts(group, contract.status_mix, contract.statuses)
        rows: list[dict[str, object]] = []
        for status in contract.statuses:
            builder = ROW_BUILDERS.get(status)
            if not builder:
                raise RuntimeError(f"No builder for status '{status}'")
            count = counts.get(status, 0)
            for idx in range(count):
                row = builder(group, status, idx, rng, data)
                rows.append(row)
        target_path = out_dir / f"workload_{group}.jsonl"
        with target_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                _ = handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        row_count = len(rows)  # type: ignore[reportUnusedCallResult]
        print(f"Generated {row_count} rows for group {group} -> {target_path}")


def validate_workload_file(
    path: Path,
    contract: WorkloadContract,
    tolerance: float,
) -> bool:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        print(f"workload invalid: file not found: {resolved}", file=sys.stderr)
        return False
    counts: Counter[str] = Counter()
    groups: set[int] = set()
    total_rows = 0
    with resolved.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = cast(dict[str, object], json.loads(line))
            status = record.get("status")
            if not isinstance(status, str) or status not in contract.statuses:
                print(
                    f"workload invalid: {resolved.name} contains unknown status '{status}'",
                    file=sys.stderr,
                )
                return False
            counts[status] += 1
            total_rows += 1
            group = record.get("group")
            if isinstance(group, int):
                groups.add(group)
    if total_rows == 0:
        print(f"workload invalid: {resolved} is empty", file=sys.stderr)
        return False
    if len(groups) > 1:
        print(
            f"workload warning: {resolved.name} contains multiple groups {sorted(groups)}",
            file=sys.stderr,
        )
    group_value = next(iter(groups)) if groups else None
    print(f"Validating {resolved.name} (group {group_value})")
    valid = True
    for status in contract.statuses:
        count = counts.get(status, 0)
        percent = (count / total_rows) * 100.0
        expected = contract.status_mix.get(status, 0)
        delta = abs(percent - expected)
        print(
            f"  {status}: {count}/{total_rows} ({percent:.1f}% vs {expected}%)",
        )
        if group_value and group_value >= 50 and delta > tolerance:
            print(
                f"ratio mismatch: {status} off by {delta:.1f}% (tolerance {tolerance}%)",
                file=sys.stderr,
            )
            valid = False
    return valid


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic workload generator/validator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen_parser = subparsers.add_parser("generate")
    _ = gen_parser.add_argument("--seed", type=int, default=1337, help="Deterministic random seed")
    _ = gen_parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write workload jsonl files",
    )
    _ = gen_parser.add_argument(
        "--groups",
        type=lambda value: tuple(int(x.strip()) for x in value.split(",") if x.strip()),
        help="Comma-separated groups to generate (defaults to contract groups)",
    )
    _ = gen_parser.add_argument(
        "--contract",
        type=Path,
        default=CONTRACT_FILE,
        help="Path to benchmark contract json",
    )
    _ = gen_parser.add_argument("--tm", type=Path, default=DEFAULT_TM_FILE, help="TM JSONL source")
    _ = gen_parser.add_argument(
        "--terms",
        type=Path,
        default=DEFAULT_TERMS_FILE,
        help="Terms CSV source",
    )
    _ = gen_parser.add_argument(
        "--po",
        type=Path,
        default=DEFAULT_PO_FILE,
        help="PO source file (JSON array of entries)",
    )

    val_parser = subparsers.add_parser("validate")
    _ = val_parser.add_argument(
        "paths",
        nargs="+",
        type=Path,
        help="Workload jsonl paths to validate",
    )
    _ = val_parser.add_argument(
        "--contract",
        type=Path,
        default=CONTRACT_FILE,
        help="Benchmark contract json",
    )
    _ = val_parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="Allowed percent difference for groups >= 50",
    )

    args = parser.parse_args()
    command = cast(str, args.command)  # type: ignore[reportAny]
    if command == "generate":
        seed = cast(int, args.seed)  # type: ignore[reportAny]
        contract_path = cast(Path, args.contract)  # type: ignore[reportAny]
        tm_path = cast(Path, args.tm)  # type: ignore[reportAny]
        terms_path = cast(Path, args.terms)  # type: ignore[reportAny]
        po_path = cast(Path, args.po)  # type: ignore[reportAny]
        output_dir = cast(Path, args.output_dir)  # type: ignore[reportAny]
        groups = cast(tuple[int, ...] | None, args.groups)  # type: ignore[reportAny]
        generate_workloads(
            seed=seed,
            contract_path=contract_path,
            tm_path=tm_path,
            terms_path=terms_path,
            po_path=po_path,
            output_dir=output_dir,
            groups=groups,
        )
        return 0

    contract_path = cast(Path, args.contract)  # type: ignore[reportAny]
    contract = load_contract(contract_path)
    success = True
    paths = cast(Sequence[Path], args.paths)  # type: ignore[reportAny]
    tolerance = cast(float, args.tolerance)  # type: ignore[reportAny]
    for path in paths:
        success &= validate_workload_file(path, contract, tolerance)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
