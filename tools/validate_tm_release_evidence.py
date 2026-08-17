#!/usr/bin/env python3
"""Recompute Feature 5 Gate A and ephemeral matcher release evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from matcher_capability import MatcherCapabilityEvaluator  # noqa: E402
from matcher_validation import recompute_matcher_validation  # noqa: E402
from tm_contracts import TextMatcherState  # noqa: E402
from tm_gate_a import recompute_gate_a  # noqa: E402


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise argparse.ArgumentTypeError(
            "timestamp must use YYYY-MM-DDTHH:MM:SSZ"
        ) from None
    return parsed.replace(tzinfo=timezone.utc)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recompute checked-in Gate A roots and a short-lived matcher "
            "manifest without persisting live evidence."
        )
    )
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=_REPOSITORY_ROOT,
    )
    parser.add_argument(
        "--approved-roots",
        type=Path,
        default=(
            _REPOSITORY_ROOT
            / "tests/fixtures/feature5_gate_a_v1.json"
        ),
    )
    parser.add_argument("--generated-at", required=True, type=_parse_utc)
    parser.add_argument("--valid-until", required=True, type=_parse_utc)
    parser.add_argument("--evaluated-at", required=True, type=_parse_utc)
    parser.add_argument(
        "--basic-only",
        action="store_true",
        help="recompute only the BASIC matcher cohort",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    gate_a = recompute_gate_a(
        repository_root=arguments.repository_root,
        approved_roots_path=arguments.approved_roots,
    )
    matcher_release = recompute_matcher_validation(
        repository_root=arguments.repository_root,
        approved_roots_path=arguments.approved_roots,
        generated_at_utc=arguments.generated_at,
        valid_until_utc=arguments.valid_until,
        include_full=not arguments.basic_only,
    )
    capability = MatcherCapabilityEvaluator(
        matcher_release.expectation
    ).evaluate(
        matcher_release.manifest,
        evaluated_at_utc=arguments.evaluated_at,
    )
    payload = {
        "gate_a": [
            {
                "component": item.component.value,
                "granted": item.granted,
                "safe_failure_code": item.safe_failure_code,
            }
            for item in gate_a.components
        ],
        "matcher": {
            "evidence_digest": (
                capability.validation_summary.evidence_digest
                if capability.validation_summary is not None
                else None
            ),
            "semantics_version": capability.semantics_version,
            "state": capability.state.value,
        },
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    gate_ok = len(gate_a.granted_components) == len(gate_a.components)
    if arguments.basic_only:
        matcher_ok = capability.state in {
            TextMatcherState.BASIC_VALIDATED,
            TextMatcherState.TEXT_V1_VALIDATED,
        }
    else:
        matcher_ok = capability.state is TextMatcherState.TEXT_V1_VALIDATED
    return int(not (gate_ok and matcher_ok))


if __name__ == "__main__":
    raise SystemExit(main())
