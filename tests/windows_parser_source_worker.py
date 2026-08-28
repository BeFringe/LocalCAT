"""Real-process WA-01 Parser source/publish termination-boundary worker."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from parser_composition import create_parser_application_surface
from parser_contracts import (
    EffectivePurpose,
    LOCALCAT_JSON_V1,
    ReadRequest,
    SelectionRequest,
    SourceReference,
    TargetReference,
)
from parser_source import atomic_write_bytes
from platform_fs_windows import WindowsPlatformAdapter
from tests.parser_io_test_support import atomic_test_write_bytes


OLD = b'[{"id":"old","source":"old","target":""}]\n'
NEW = b'[{"id":"new","source":"new","target":"done"}]\n'


def _materialized_id(root: Path, target: Path) -> str:
    surface = create_parser_application_surface()
    opened = surface.open_input(
        SourceReference(str(root), str(target), target.name),
        SelectionRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
        ReadRequest(EffectivePurpose.PROJECT_DOCUMENT, LOCALCAT_JSON_V1),
    )
    if not hasattr(opened, "materialize"):
        raise RuntimeError("Parser selection failed")
    with opened:
        result = opened.materialize()
    return result.records[0].local_id


def publish(root: Path, target: Path, fault_phase: str | None) -> None:
    def terminate(phase: str) -> None:
        if phase == fault_phase:
            os._exit(87)

    adapter = WindowsPlatformAdapter(_fault_injector=terminate if fault_phase else None)
    atomic_test_write_bytes(
        TargetReference(str(root), str(target), target.name),
        NEW,
        backend=adapter,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("publish", "reopen"))
    parser.add_argument("root", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--fault-phase")
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise RuntimeError("Windows Parser worker requires Windows")
    if args.action == "publish":
        publish(args.root, args.target, args.fault_phase)
        return 0
    print(json.dumps({"local_id": _materialized_id(args.root, args.target)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
