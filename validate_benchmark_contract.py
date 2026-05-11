import json
import sys
from typing import cast


REQUIRED_CONFIG_KEYS = {
    "groups",
    "repeats",
    "warmup",
    "warm_cold_modes",
    "status_mix",
}

REQUIRED_METRICS = {
    "init_ms",
    "total_ms",
    "per_row_us_median",
    "per_row_us_p95",
    "throughput_rows_s",
}

REQUIRED_GROUPS = [5, 50, 200, 800]
REQUIRED_STATUS_MIX = {"TM_HIT": 60, "TERMS_FOUND": 25, "NO_MATCH": 15}


def _missing_key(path: str) -> int:
    print(f"contract invalid: missing key '{path}'", file=sys.stderr)
    return 1


def _invalid(path: str, detail: str) -> int:
    print(f"contract invalid: {path}: {detail}", file=sys.stderr)
    return 1


def _as_dict(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return cast(dict[str, object], value)


def _as_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{path} must be a list")
    return cast(list[object], value)


def _require_key(obj: dict[str, object], key: str, path: str) -> object:
    if key not in obj:
        raise KeyError(path)
    return obj[key]


def _require_list_of_strings(obj: dict[str, object], key: str, path: str) -> list[str]:
    raw = _as_list(_require_key(obj, key, path), path)
    out: list[str] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, str):
            raise ValueError(f"{path}[{idx}] must be a string")
        out.append(item)
    return out


def _require_int(obj: dict[str, object], key: str, path: str) -> int:
    raw = _require_key(obj, key, path)
    if not isinstance(raw, int):
        raise ValueError(f"{path} must be an integer")
    return raw


def validate_contract(contract: dict[str, object]) -> int:
    try:
        runtime_schema = _as_dict(
            _require_key(contract, "runtime_schema_source", "runtime_schema_source"),
            "runtime_schema_source",
        )
        benchmark_config = _as_dict(
            _require_key(contract, "benchmark_config", "benchmark_config"),
            "benchmark_config",
        )

        _ = _require_key(runtime_schema, "controller_method", "runtime_schema_source.controller_method")
        _ = _require_key(runtime_schema, "adapter_usage_file", "runtime_schema_source.adapter_usage_file")

        statuses = _require_list_of_strings(runtime_schema, "statuses", "runtime_schema_source.statuses")
        if statuses != ["TM_HIT", "TERMS_FOUND", "NO_MATCH"]:
            return _invalid(
                "runtime_schema_source.statuses",
                f"expected ['TM_HIT', 'TERMS_FOUND', 'NO_MATCH'], got {statuses}",
            )

        payload = _as_dict(
            _require_key(
                runtime_schema,
                "payload_keys_by_status",
                "runtime_schema_source.payload_keys_by_status",
            ),
            "runtime_schema_source.payload_keys_by_status",
        )

        tm_hit = _as_dict(
            _require_key(payload, "TM_HIT", "runtime_schema_source.payload_keys_by_status.TM_HIT"),
            "runtime_schema_source.payload_keys_by_status.TM_HIT",
        )
        if _require_list_of_strings(
            tm_hit,
            "required_top_level",
            "runtime_schema_source.payload_keys_by_status.TM_HIT.required_top_level",
        ) != ["status", "tm_match"]:
            return _invalid(
                "runtime_schema_source.payload_keys_by_status.TM_HIT.required_top_level",
                "expected ['status', 'tm_match']",
            )
        if _require_list_of_strings(
            tm_hit,
            "required_tm_match_keys",
            "runtime_schema_source.payload_keys_by_status.TM_HIT.required_tm_match_keys",
        ) != ["source", "target", "match_type", "similarity"]:
            return _invalid(
                "runtime_schema_source.payload_keys_by_status.TM_HIT.required_tm_match_keys",
                "expected ['source', 'target', 'match_type', 'similarity']",
            )

        terms_found = _as_dict(
            _require_key(
                payload,
                "TERMS_FOUND",
                "runtime_schema_source.payload_keys_by_status.TERMS_FOUND",
            ),
            "runtime_schema_source.payload_keys_by_status.TERMS_FOUND",
        )
        if _require_list_of_strings(
            terms_found,
            "required_top_level",
            "runtime_schema_source.payload_keys_by_status.TERMS_FOUND.required_top_level",
        ) != ["status", "terms"]:
            return _invalid(
                "runtime_schema_source.payload_keys_by_status.TERMS_FOUND.required_top_level",
                "expected ['status', 'terms']",
            )
        if _require_list_of_strings(
            terms_found,
            "required_term_item_keys",
            "runtime_schema_source.payload_keys_by_status.TERMS_FOUND.required_term_item_keys",
        ) != ["source_term", "target_term", "start_index", "end_index", "glossary_source"]:
            return _invalid(
                "runtime_schema_source.payload_keys_by_status.TERMS_FOUND.required_term_item_keys",
                "expected ['source_term', 'target_term', 'start_index', 'end_index', 'glossary_source']",
            )

        no_match = _as_dict(
            _require_key(payload, "NO_MATCH", "runtime_schema_source.payload_keys_by_status.NO_MATCH"),
            "runtime_schema_source.payload_keys_by_status.NO_MATCH",
        )
        if _require_list_of_strings(
            no_match,
            "required_top_level",
            "runtime_schema_source.payload_keys_by_status.NO_MATCH.required_top_level",
        ) != ["status"]:
            return _invalid(
                "runtime_schema_source.payload_keys_by_status.NO_MATCH.required_top_level",
                "expected ['status']",
            )

        missing_config = sorted(REQUIRED_CONFIG_KEYS - set(benchmark_config.keys()))
        if missing_config:
            return _missing_key(f"benchmark_config.{missing_config[0]}")

        groups_raw = _as_list(benchmark_config["groups"], "benchmark_config.groups")
        groups: list[int] = []
        for idx, item in enumerate(groups_raw):
            if not isinstance(item, int):
                raise ValueError(f"benchmark_config.groups[{idx}] must be an integer")
            groups.append(item)
        if groups != REQUIRED_GROUPS:
            return _invalid("benchmark_config.groups", f"expected {REQUIRED_GROUPS}, got {groups}")

        repeats = _require_int(benchmark_config, "repeats", "benchmark_config.repeats")
        if repeats < 5:
            return _invalid("benchmark_config.repeats", f"must be integer >=5, got {repeats}")

        warmup = _require_int(benchmark_config, "warmup", "benchmark_config.warmup")
        if warmup < 1:
            return _invalid("benchmark_config.warmup", f"must be integer >=1, got {warmup}")

        status_mix = _as_dict(benchmark_config["status_mix"], "benchmark_config.status_mix")
        for status_name, expected_value in REQUIRED_STATUS_MIX.items():
            if status_name not in status_mix:
                return _missing_key(f"benchmark_config.status_mix.{status_name}")
            actual_value = status_mix[status_name]
            if not isinstance(actual_value, int):
                raise ValueError(f"benchmark_config.status_mix.{status_name} must be an integer")
            if actual_value != expected_value:
                return _invalid(
                    f"benchmark_config.status_mix.{status_name}",
                    f"expected {expected_value}, got {actual_value}",
                )
        status_mix_total = 0
        for value in status_mix.values():
            if not isinstance(value, int):
                raise ValueError("benchmark_config.status_mix values must be integers")
            status_mix_total += value
        if status_mix_total != 100:
            return _invalid("benchmark_config.status_mix", "sum must be 100")

        metrics_raw = _as_list(_require_key(contract, "required_metrics", "required_metrics"), "required_metrics")
        metrics: set[str] = set()
        for idx, metric in enumerate(metrics_raw):
            if not isinstance(metric, str):
                raise ValueError(f"required_metrics[{idx}] must be a string")
            metrics.add(metric)
        missing_metrics = sorted(REQUIRED_METRICS - metrics)
        if missing_metrics:
            return _missing_key(f"required_metrics.{missing_metrics[0]}")

        return 0
    except KeyError as missing_path:
        return _missing_key(str(missing_path))
    except ValueError as exc:
        return _invalid("schema", str(exc))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python validate_benchmark_contract.py <contract_path>", file=sys.stderr)
        return 2

    contract_path = sys.argv[1]
    try:
        with open(contract_path, "r", encoding="utf-8") as handle:
            raw_contract = cast(object, json.load(handle))
    except FileNotFoundError:
        print(f"contract invalid: file not found: {contract_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"contract invalid: bad json: {exc}", file=sys.stderr)
        return 1

    if not isinstance(raw_contract, dict):
        print("contract invalid: schema: root must be an object", file=sys.stderr)
        return 1

    contract = cast(dict[str, object], raw_contract)
    result = validate_contract(contract)
    if result == 0:
        print("contract valid")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
