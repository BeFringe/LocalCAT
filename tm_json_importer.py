import argparse
import json
from collections import OrderedDict
from pathlib import Path
from typing import cast


BASE_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import normalized TM JSON into the JSONL format used by LocalCAT Phase-3."
    )
    _ = parser.add_argument(
        "--input",
        nargs="+",
        required=True,
        help="One or more normalized TM JSON files or directories containing JSON files.",
    )
    _ = parser.add_argument(
        "--output",
        default=str(BASE_DIR / "tm.jsonl"),
        help="Output JSONL path. Defaults to CAT/tm.jsonl.",
    )
    return parser.parse_args()


def resolve_input_files(raw_inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw_input in raw_inputs:
        path = Path(raw_input).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Input path not found: {path}")
        if path.is_dir():
            files.extend(sorted(child for child in path.iterdir() if child.suffix.lower() == ".json"))
        elif path.suffix.lower() == ".json":
            files.append(path)
        else:
            raise ValueError(f"Unsupported input path: {path}")
    if not files:
        raise ValueError("No JSON files found in the provided input paths.")
    return files


def normalize_record(entry: object, source_name: str) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    entry_dict = cast(dict[str, object], entry)
    source = entry_dict.get("source", "")
    target = entry_dict.get("target", "")
    speaker = entry_dict.get("speaker", "")
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    normalized_source = source.strip()
    normalized_target = target.strip()
    if not normalized_source or not normalized_target:
        return None
    normalized_speaker = speaker.strip() if isinstance(speaker, str) else ""
    return {
        "source": normalized_source,
        "target": normalized_target,
        "speaker": normalized_speaker,
        "file_source": source_name,
    }


def load_records(input_files: list[Path]) -> OrderedDict[str, dict[str, str]]:
    records_by_source: OrderedDict[str, dict[str, str]] = OrderedDict()
    for input_file in input_files:
        payload_obj = cast(object, json.loads(input_file.read_text(encoding="utf-8")))
        if not isinstance(payload_obj, list):
            raise ValueError(f"Expected a JSON array in {input_file}")
        payload_entries = cast(list[object], payload_obj)
        for entry in payload_entries:
            record = normalize_record(entry, input_file.name)
            if record is None:
                continue
            source = record["source"]
            if source in records_by_source:
                del records_by_source[source]
            records_by_source[source] = record
    return records_by_source


def write_jsonl(output_path: Path, records_by_source: OrderedDict[str, dict[str, str]]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in records_by_source.values():
            _ = handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    input_paths = cast(list[str], args.input)
    output_raw = cast(str, args.output)
    input_files = resolve_input_files(input_paths)
    records_by_source = load_records(input_files)
    output_path = Path(output_raw).expanduser().resolve()
    write_jsonl(output_path, records_by_source)
    print(f"Imported {len(input_files)} JSON file(s).")
    print(f"Wrote {len(records_by_source)} TM records to {output_path}")
    return 0


if __name__ == "__main__":
    _ = main()
