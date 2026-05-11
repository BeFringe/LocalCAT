#!/usr/bin/env python3
"""Malformed backend artifact test for comparative report generator."""

import json
import os
import sys
from pathlib import Path


def create_malformed_artifact() -> str:
    artifact_dir = Path("artifacts/perf")
    backend_files = list(artifact_dir.glob("backend_throughput_*.json"))
    if not backend_files:
        raise FileNotFoundError("No backend_throughput_*.json artifact found in artifacts/perf")

    source_path = max(backend_files, key=os.path.getmtime)
    with source_path.open("r", encoding="utf-8") as handle:
        valid_artifact = json.load(handle)

    for row in valid_artifact.get("results", []):
        if "per_row_us_p95" in row:
            del row["per_row_us_p95"]
            break
    else:
        raise ValueError("Source artifact has no per_row_us_p95 field to remove")

    malformed_path = artifact_dir / "backend_throughput_missing_per_row_us_p95.json"
    with malformed_path.open("w", encoding="utf-8") as handle:
        json.dump(valid_artifact, handle, indent=2)

    return str(malformed_path)


def test_malformed_artifact() -> int:
    malformed_path = create_malformed_artifact()

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from generate_comparative_report import extract_backend_group_data

    try:
        with open(malformed_path, "r", encoding="utf-8") as handle:
            malformed_artifact = json.load(handle)

        first_row = malformed_artifact["results"][0]
        extract_backend_group_data(
            malformed_artifact["results"],
            int(first_row["group"]),
            str(first_row["mode"]),
        )
    except KeyError as exc:
        print(f"Malformed artifact test PASSED: missing field detected ({exc})")
        os.remove(malformed_path)
        return 0
    except Exception as exc:
        print(f"Malformed artifact test FAILED with unexpected error: {exc}")
        os.remove(malformed_path)
        return 1

    print("Malformed artifact test FAILED: expected missing-field failure path")
    os.remove(malformed_path)
    return 1


if __name__ == "__main__":
    sys.exit(test_malformed_artifact())
