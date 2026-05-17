# Technology Stack

## Architecture

Strict 4-layer architecture with zero upward dependencies. Each layer only calls the layer directly below it.

| Layer | Name | Responsibility | Key Files |
|-------|------|---------------|-----------|
| Layer 1 | Storage | JSONL/CSV/PO data persistence, streaming I/O | `tm_engine.py` (TMEngine), `tm_json_importer.py` |
| Layer 2 | Core Engine | Pure business logic, stateless algorithms | `glossary_engine.py` (Trie), `tm_engine.py` (TMEngine, POHandler) |
| Layer 3 | Logic UI | Stateless request-response forwarder, TM > Glossary priority | `logic_controller.py` |
| Layer 4 | Frontend | User interaction, data display | `excel_adapter.py` (xlwings), `excel_adapter_openpyxl.py` (openpyxl) |

**Critical constraint**: Layer 1-2 has a hard dependency wall — no `xlwings`, `PySide6`, or any UI library allowed. `logic_controller.py` contains zero state variables.

## Core Technologies

- **Language**: Python 3.14+ (currently running on 3.14.3)
- **No package config** — No pyproject.toml, setup.py, or requirements.txt currently exists
- **Runtime**: Local-only, no server component

## Key Libraries

**Stdlib-only core (Layer 1-3)** — No external dependencies for core engine layers:
- `dataclasses` (`frozen=True`) — Immutable cross-layer data contracts
- `json` / `pathlib` — JSONL read/write, file system operations
- `csv` — Glossary CSV loading
- `collections` (Counter, OrderedDict) — Deduplication, counting
- `time` (`perf_counter_ns`) — High-resolution timing for benchmarks

**External (Layer 4 only)**:
- `openpyxl` — File-mode Excel read/write in batch adapter
- `xlwings` — Interactive Excel adapter. > 📎 导入隔离策略详见 ADR-003

**Planned (not yet implemented)**:
- `PySide6` — QT Desktop editor (Feature 4)
- Levenshtein/Dice coefficient libraries — Fuzzy matching (Feature 5)

## Development Standards

### Type Safety
- `@dataclass(frozen=True)` for all cross-layer data contracts — immutability is mandatory
- `from __future__ import annotations` in newer files (PEP 604 union syntax `X | Y`)
- `typing.cast()` for explicit type narrowing in benchmark tooling
- **No type-checking config files** (no mypy.ini, pyrightconfig.json)
- Inline pyright directives in a few benchmark tooling files

### Code Quality
- No formal linter/formatter config (no .flake8, .pylintrc, tox.ini, pre-commit)
- Enforced through spec contracts and code review convention
- Section headers (`# ===...`) for internal file structure

### Testing
- **Self-test pattern**: Every core module has `if __name__ == "__main__":` block with assert-based tests
- **Benchmark contract system**: JSON schema (`benchmark_contract.json`) defines runtime expectations; multiple validators enforce compliance
- **Integration runners**: `stress_runner.py` (structural integrity), `translation_runner.py` (end-to-end data flow)
- **Scaling gate**: Linearity threshold checks (p95 growth ratio ≤ 2.5, median ≤ 2.0)
- No formal test framework (no pytest, unittest)

## Development Environment

### Required Tools
- Python 3.14+
- openpyxl (for Excel batch adapter)
- xlwings (optional, for interactive Excel adapter)

### Common Commands
```bash
# Self-tests (each module is self-validating)
python glossary_engine.py          # Glossary engine self-tests
python tm_engine.py                # TM engine self-tests
python logic_controller.py         # Logic controller self-tests

# Integration testing
python stress_runner.py            # Structural integrity stress test
python translation_runner.py       # End-to-end integration verification

# Excel adapters
python excel_adapter_openpyxl.py --input-xlsx <file> [options]  # Batch mode

# Benchmark pipeline
python deterministic_workload.py generate [options]     # Generate workloads
python backend_throughput_harness.py [options]          # Run throughput benchmark
python backend_scaling_gate.py --backend-artifact <path>  # Scaling linearity gate
python validate_benchmark_contract.py <contract_path>   # Validate contract schema
```

## Key Technical Decisions

| Decision | Rationale |
|----------|-----------|
| **Glossary matching backend** | > 📎 详见 ADR-001 |
| **TM storage backend** | > 📎 详见 ADR-002 |
| **Frozen dataclasses for cross-layer data** | Prevents accidental mutation, explicit data flow |
| **Stateless LogicController** | No history/state variables; any frontend calls it identically |
| **TM-priority strategy** (TM > Glossary) | Exact match skips glossary extraction; avoids redundant noise |
| **Dual Excel adapters** | xlwings (interactive) + openpyxl (headless). > 📎 xlwings 隔离策略详见 ADR-003 |
| **Conditional openpyxl import** | Core engine degrades gracefully if openpyxl not installed |
| **No external deps for core** | Layer 1-3 use only stdlib; zero install friction |
| **Benchmark contract as schema enforcer** | Single source of truth for runtime expectations; prevents regressions |
| **Deterministic seeded workloads** | `random.Random(seed=1337)` for reproducible cross-run comparison |

## Data Formats

| Format | Purpose |
|--------|---------|
| CSV (utf-8-sig) | Glossary term lists |
| JSONL (append-only) | Translation Memory storage, benchmark workloads |
| PO (gettext) | Source translation units |
| JSON | Benchmark artifacts, contracts, reports |
| XLSX/XLS | Excel glossary input and output workbooks |

---
_Document standards and patterns, not every dependency_
