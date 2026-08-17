"""Deterministic benchmark-v1 corpus, cohort, and oracle input owner.

Ownership (Task 8.1)
--------------------
This module owns the deterministic inputs for the benchmark-v1 gate: the
100,000-record corpus, the exact/fuzzy query cohorts, the fixed 5,000-record /
200-query oracle subset, and the machine-readable ``BenchmarkContract`` JSON
payload.  It is an offline validation/batch owner only: no production runtime
module imports it, and this slice performs no latency/RSS measurement, no
candidate recall/oracle scoring, and no Gate D publication (Tasks 8.2-8.5).

Dependencies are limited to the standard library and frozen contracts
(``tm_contracts``).  In particular, no store, retrieval, migration, matcher,
capability, Qt, or Feature3 benchmark modules are imported.

Determinism and digests
-----------------------
Every corpus record is a pure function of ``(seed, record_id)``; every query is
a pure function of the seed, the record plan, and its position in the cohort.
No runtime/environment facts (host, CPU, FTS availability, wall clock) enter
the generated values or digests.

Digests use one canonical, versioned, closed framing:

    sha256( header + "\\n" + item_json + "\\n" + item_json + ... )

where ``header`` is the canonical JSON object
``{"digest_schema": "tm-benchmark-digest-v1", "kind": KIND,
"generator_version": GENERATOR_VERSION}`` and every ``item_json`` is
``json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
sort_keys=True)``.  Kinds: ``corpus``, ``corpus-composition``,
``exact-cohort``, ``fuzzy-cohort``, ``oracle-subset``, ``scorer-config``,
``path-config``.

Corpus composition
------------------
Records 1..100000 are generated in ascending id order:

- 1..2000          : CJK (zh/ja/ko) content.
- 2001..5000       : short records (source and target at most 12 code points).
- 5001..8000       : duplicate raw-source / multi-target groups (300 groups of
                     10 records; each group shares one source and three
                     distinct targets).
- 8001..13000      : context records (speaker/prev/next all set).
- 13001..15000     : near-edit record pairs (1000 base/edited pairs whose
                     sources differ by exactly one character substitution).
- 15001..100000    : multilingual filler across 10 fixed language pools.

Category counts in ``corpus_composition_digest`` are computed from generated
facts (content, field presence, and full-corpus source grouping), not from
self-reported labels; near-edit pairs are verified during streaming.

Cohorts
-------
- exact cohort: 1200 queries; query ``i`` references record ``(i*83) % 100000 +
  1`` and its query text is that record's source (deterministic winner).
- fuzzy cohort: 240 queries = 200 near-edit (one-character substitution of a
  referenced record source) + 40 miss (reserved ``zzmissf5v1`` marker, absent
  from the whole corpus).
- oracle subset: records ``(i*17) % 100000 + 1`` for i in 1..5000, ascending;
  200 queries = 160 exact + 20 near-edit + 20 miss, all references drawn from
  the oracle subset.

``recompute_benchmark_inputs`` loads the committed contract strictly and
compares every recomputed digest/count against it; any mismatch fails closed.
The generator never trusts caller booleans or the JSON's digest values as
truth, and never writes output during generation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from tm_contracts import (
    BENCHMARK_CONTRACT_VERSION,
    BENCHMARK_PERCENTILE_METHOD,
    BENCHMARK_RSS_SCOPE,
    CANDIDATE_BUDGET_VERSION,
    CANDIDATE_PROOF_QUERY_VERSION,
    SCORER_VERSION_V1,
    BenchmarkContract,
    TMRecordDraft,
    contract_from_json,
)

TM_BENCHMARK_CORPUS_VERSION = "tm-benchmark-corpus-v1"
TM_BENCHMARK_COMPOSITION_VERSION = "tm-corpus-composition-v1"
TM_BENCHMARK_DIGEST_SCHEMA = "tm-benchmark-digest-v1"
TM_BENCHMARK_SCORER_CONFIG_VERSION = "scorer-config-v1"
TM_BENCHMARK_PATH_CONFIG_VERSION = "benchmark-path-config-v1"
BENCHMARK_IMPLEMENTATION_FINGERPRINT_VERSION = (
    "tm-benchmark-implementation-fingerprint-v1"
)
BENCHMARK_IMPLEMENTATION_SOURCE_PATHS = (
    "benchmark_tm_contract.json",
    "capability_gated_text_matcher.py",
    "matcher_capability.py",
    "text_matcher.py",
    "tm_activation_journal.py",
    "tm_activation_recovery.py",
    "tm_benchmark.py",
    "tm_benchmark_gate.py",
    "tm_benchmark_latency.py",
    "tm_benchmark_oracle.py",
    "tm_benchmark_process.py",
    "tm_benchmark_query_process.py",
    "tm_candidate_index.py",
    "tm_content_attestation.py",
    "tm_contracts.py",
    "tm_gate_b.py",
    "tm_migration.py",
    "tm_retrieval.py",
    "tm_retrieval_capability.py",
    "tm_schema_upgrade.py",
    "tm_similarity.py",
    "tm_snapshot_artifacts.py",
    "tm_snapshot_recovery.py",
    "tm_sqlite_store.py",
    "tm_stage_sealer.py",
    "unicode_word_break_data.py",
)
if BENCHMARK_IMPLEMENTATION_SOURCE_PATHS != tuple(
    sorted(set(BENCHMARK_IMPLEMENTATION_SOURCE_PATHS))
):
    raise RuntimeError("benchmark implementation source closure is invalid")

_NATIVE_PATH_TYPE = type(Path())

TM_BENCHMARK_DEFAULT_SEED = 20260729
TM_BENCHMARK_CORPUS_RECORD_COUNT = 100_000
TM_BENCHMARK_EXACT_MIN_SAMPLES = 1_000
TM_BENCHMARK_EXACT_COHORT_COUNT = 1_200
TM_BENCHMARK_FUZZY_MIN_SAMPLES = 200
TM_BENCHMARK_FUZZY_COHORT_COUNT = 240
TM_BENCHMARK_ORACLE_SUBSET_RECORD_COUNT = 5_000
TM_BENCHMARK_ORACLE_QUERY_COUNT = 200
TM_BENCHMARK_TOP_K = 10
TM_BENCHMARK_MINIMUM_SIMILARITY = 0.60
TM_BENCHMARK_WARMUP_QUERIES_PER_COHORT = 100
TM_BENCHMARK_MEASURED_REPEATS = 1
TM_BENCHMARK_EXACT_P95_GATE_MS = 50.0
TM_BENCHMARK_FUZZY_P95_GATE_MS = 500.0
TM_BENCHMARK_MIGRATION_GATE_SECONDS = 120.0
TM_BENCHMARK_PEAK_RSS_GATE_MIB = 512.0
TM_BENCHMARK_CANDIDATE_RECALL_GATE = 1.0

TM_BENCHMARK_MISS_PREFIX = "zzmissf5v1"

_CJK_RANGE = (1, 2_000)
_SHORT_RANGE = (2_001, 5_000)
_DUPLICATE_RANGE = (5_001, 8_000)
_DUPLICATE_GROUP_SIZE = 10
_DUPLICATE_TARGET_VARIANTS = 3
_CONTEXT_RANGE = (8_001, 13_000)
_NEAR_EDIT_RANGE = (13_001, 15_000)
_SHORT_MAX_LENGTH = 12

_LANGUAGES = (
    "en",
    "fr",
    "de",
    "es",
    "it",
    "pt",
    "ru",
    "zh",
    "ja",
    "ko",
)
_CJK_LANGUAGES = ("zh", "ja", "ko")

_SHORT_SOURCES = (
    "Go.",
    "Wait.",
    "Stop.",
    "Exit.",
    "Open.",
    "Save.",
    "Close.",
    "Yes.",
    "No.",
    "Run.",
    "Help.",
    "Quit.",
)
_SHORT_TARGETS = (
    "走吧。",
    "等等。",
    "停止。",
    "退出。",
    "打开。",
    "保存。",
    "关闭。",
    "是。",
    "否。",
    "运行。",
    "帮助。",
    "退出。",
)

_CJK_NOUNS = ("翻译", "记录", "条目", "句子", "索引")
_CJK_TEMPLATES = {
    "zh": (
        "系统处理了{noun}并保存了结果。",
        "系统将{noun}的结果保存到本地。",
    ),
    "ja": (
        "システムは{noun}を処理して結果を保存しました。",
        "システムは{noun}の結果をローカルに保存しました。",
    ),
    "ko": (
        "시스템이 {noun}을 처리하고 결과를 저장했습니다.",
        "시스템이 {noun}의 결과를 로컬에 저장했습니다.",
    ),
}

_LANG_TEMPLATES = {
    "en": ("The {adj} {noun} {verb} the {obj}.", "The {obj} is {verb} by the {adj} {noun}."),
    "fr": ("Le {noun} {adj} {verb} le {obj}.", "Le {obj} est {verb} par le {noun} {adj}."),
    "de": ("Das {adj} {noun} {verb} das {obj}.", "Das {obj} wird von dem {adj} {noun} {verb}."),
    "es": ("El {noun} {adj} {verb} el {obj}.", "El {obj} es {verb} por el {noun} {adj}."),
    "it": ("Il {noun} {adj} {verb} il {obj}.", "Il {obj} è {verb} dal {noun} {adj}."),
    "pt": ("O {noun} {adj} {verb} o {obj}.", "O {obj} é {verb} pelo {noun} {adj}."),
    "ru": ("{adj} {noun} {verb} {obj}.", "{obj} {verb} {adj} {noun}."),
    "zh": ("{adj}{noun}{verb}{obj}。", "{obj}由{adj}{noun}{verb}。"),
    "ja": ("{adj}{noun}は{obj}を{verb}。", "{obj}は{adj}{noun}によって{verb}。"),
    "ko": ("{adj}{noun}가 {obj}를 {verb}.", "{obj}는 {adj}{noun}에 의해 {verb}."),
}
_LANG_WORDS = {
    "en": {
        "adj": ("local", "stable", "fast", "safe"),
        "noun": ("signal", "record", "entry", "phrase"),
        "verb": ("stores", "reads", "indexes", "validates"),
        "obj": ("result", "target", "source", "cache"),
    },
    "fr": {
        "adj": ("local", "stable", "rapide", "sûr"),
        "noun": ("signal", "enregistrement", "entrée", "phrase"),
        "verb": ("stocke", "lit", "indexe", "valide"),
        "obj": ("résultat", "cible", "source", "cache"),
    },
    "de": {
        "adj": ("lokal", "stabil", "schnell", "sicher"),
        "noun": ("Signal", "Eintrag", "Satz", "Datensatz"),
        "verb": ("speichert", "liest", "indiziert", "validiert"),
        "obj": ("Ergebnis", "Ziel", "Quelle", "Cache"),
    },
    "es": {
        "adj": ("local", "estable", "rápido", "seguro"),
        "noun": ("señal", "registro", "entrada", "frase"),
        "verb": ("almacena", "lee", "indexa", "valida"),
        "obj": ("resultado", "destino", "fuente", "caché"),
    },
    "it": {
        "adj": ("locale", "stabile", "veloce", "sicuro"),
        "noun": ("segnale", "voce", "frase", "record"),
        "verb": ("salva", "legge", "indicizza", "valida"),
        "obj": ("risultato", "destinazione", "origine", "cache"),
    },
    "pt": {
        "adj": ("local", "estável", "rápido", "seguro"),
        "noun": ("sinal", "registro", "entrada", "frase"),
        "verb": ("armazena", "lê", "indexa", "valida"),
        "obj": ("resultado", "destino", "origem", "cache"),
    },
    "ru": {
        "adj": ("локальный", "стабильный", "быстрый", "безопасный"),
        "noun": ("сигнал", "запись", "строка", "фраза"),
        "verb": ("хранит", "читает", "индексирует", "проверяет"),
        "obj": ("результат", "цель", "источник", "кэш"),
    },
    "zh": {
        "adj": ("本地", "稳定", "快速", "安全"),
        "noun": ("信号", "记录", "条目", "短语"),
        "verb": ("存储", "读取", "索引", "校验"),
        "obj": ("结果", "目标", "来源", "缓存"),
    },
    "ja": {
        "adj": ("ローカル", "安定", "高速", "安全"),
        "noun": ("信号", "記録", "項目", "フレーズ"),
        "verb": ("保存する", "読み取る", "索引する", "検証する"),
        "obj": ("結果", "ターゲット", "ソース", "キャッシュ"),
    },
    "ko": {
        "adj": ("로컬", "안정", "빠른", "안전한"),
        "noun": ("신호", "기록", "항목", "구문"),
        "verb": ("저장하다", "읽다", "색인하다", "검증하다"),
        "obj": ("결과", "대상", "소스", "캐시"),
    },
}

_MASK64 = (1 << 64) - 1
_MIX_MULT_1 = 0xBF58476D1CE4E5B9
_MIX_MULT_2 = 0x94D049BB133111EB
_GOLDEN_RATIO_64 = 0x9E3779B97F4A7C15
_SHA256_DIGEST = re.compile(r"[0-9a-f]{64}\Z")

_COHORTS = ("exact", "fuzzy", "oracle")
_CATEGORIES = ("exact", "near-edit", "miss")


def _mix64(value: int) -> int:
    value = (value ^ (value >> 30)) * _MIX_MULT_1 & _MASK64
    value = (value ^ (value >> 27)) * _MIX_MULT_2 & _MASK64
    return value ^ (value >> 31)


def _derive(seed: int, salt: int, index: int) -> int:
    mixed = (seed ^ salt) + index * _GOLDEN_RATIO_64
    return _mix64(mixed & _MASK64)


def _require_int(
    value: object,
    field_name: str,
    *,
    minimum: int,
) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")


def _require_identity(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _validate_generation_params(
    seed: object,
    record_count: object,
) -> None:
    _require_int(seed, "seed", minimum=0)
    _require_int(record_count, "record count", minimum=1)


def _pick(pool: tuple[str, ...], derived: int) -> str:
    return pool[derived % len(pool)]


def _one_char_edit(text: str) -> str:
    """Return text with exactly one character substituted at the last position."""
    if not text:
        raise ValueError("cannot apply a one-character edit to empty text")
    last = text[-1]
    replacement = "x" if last != "x" else "y"
    return text[:-1] + replacement


def _language_for(seed: int, record_id: int) -> str:
    if _CJK_RANGE[0] <= record_id <= _CJK_RANGE[1]:
        return _CJK_LANGUAGES[record_id % len(_CJK_LANGUAGES)]
    return _LANGUAGES[_derive(seed, 101, record_id) % len(_LANGUAGES)]


def _multilingual_pair(
    seed: int,
    record_id: int,
    salt: int,
) -> tuple[str, str]:
    language = _language_for(seed, record_id)
    source_template, target_template = _LANG_TEMPLATES[language]
    words = _LANG_WORDS[language]
    derived = _derive(seed, salt, record_id)
    values = {
        "adj": _pick(words["adj"], derived),
        "noun": _pick(words["noun"], derived >> 8),
        "verb": _pick(words["verb"], derived >> 16),
        "obj": _pick(words["obj"], derived >> 24),
    }
    return source_template.format(**values), target_template.format(**values)


def _cjk_pair(seed: int, record_id: int) -> tuple[str, str]:
    language = _CJK_LANGUAGES[record_id % len(_CJK_LANGUAGES)]
    noun = _pick(_CJK_NOUNS, _derive(seed, 777, record_id))
    source_template, target_template = _CJK_TEMPLATES[language]
    return source_template.format(noun=noun), target_template.format(noun=noun)


def _duplicate_group_index(record_id: int) -> int:
    return (record_id - _DUPLICATE_RANGE[0]) // _DUPLICATE_GROUP_SIZE


def _duplicate_variant(record_id: int) -> int:
    return (record_id - _DUPLICATE_RANGE[0]) % _DUPLICATE_GROUP_SIZE


def _duplicate_source(seed: int, record_id: int) -> str:
    group = _duplicate_group_index(record_id)
    noun = _pick(_LANG_WORDS["en"]["noun"], _derive(seed, 303, group))
    return f"Shared source sentence {group} about {noun}."


def _duplicate_target(seed: int, record_id: int) -> str:
    group = _duplicate_group_index(record_id)
    variant = _duplicate_variant(record_id) % _DUPLICATE_TARGET_VARIANTS + 1
    noun = _pick(_LANG_WORDS["en"]["noun"], _derive(seed, 303, group))
    return f"Shared target variant {variant} for {noun}."


def _is_near_edit_base(record_id: int) -> bool:
    return (
        _NEAR_EDIT_RANGE[0] <= record_id <= _NEAR_EDIT_RANGE[1]
        and (record_id - _NEAR_EDIT_RANGE[0]) % 2 == 0
    )


def _source_for(seed: int, record_id: int) -> str:
    if _CJK_RANGE[0] <= record_id <= _CJK_RANGE[1]:
        return _cjk_pair(seed, record_id)[0]
    if _SHORT_RANGE[0] <= record_id <= _SHORT_RANGE[1]:
        return _SHORT_SOURCES[
            (record_id - _SHORT_RANGE[0]) % len(_SHORT_SOURCES)
        ]
    if _DUPLICATE_RANGE[0] <= record_id <= _DUPLICATE_RANGE[1]:
        return _duplicate_source(seed, record_id)
    if _CONTEXT_RANGE[0] <= record_id <= _CONTEXT_RANGE[1]:
        return _multilingual_pair(seed, record_id, 901)[0] + f" [ctx {record_id}]"
    if _NEAR_EDIT_RANGE[0] <= record_id <= _NEAR_EDIT_RANGE[1]:
        base_id = _NEAR_EDIT_RANGE[0] + (
            (record_id - _NEAR_EDIT_RANGE[0]) // 2
        ) * 2
        base_source = _multilingual_pair(seed, base_id, 902)[0] + (
            f" #near{base_id}"
        )
        if record_id == base_id:
            return base_source
        return _one_char_edit(base_source)
    return _multilingual_pair(seed, record_id, 1001)[0] + f" (#{record_id})"


def _target_for(seed: int, record_id: int) -> str:
    if _CJK_RANGE[0] <= record_id <= _CJK_RANGE[1]:
        return _cjk_pair(seed, record_id)[1]
    if _SHORT_RANGE[0] <= record_id <= _SHORT_RANGE[1]:
        return _SHORT_TARGETS[
            (record_id - _SHORT_RANGE[0]) % len(_SHORT_TARGETS)
        ]
    if _DUPLICATE_RANGE[0] <= record_id <= _DUPLICATE_RANGE[1]:
        return _duplicate_target(seed, record_id)
    if _CONTEXT_RANGE[0] <= record_id <= _CONTEXT_RANGE[1]:
        return _multilingual_pair(seed, record_id, 901)[1] + f" [ctx {record_id}]"
    if _NEAR_EDIT_RANGE[0] <= record_id <= _NEAR_EDIT_RANGE[1]:
        base_id = _NEAR_EDIT_RANGE[0] + (
            (record_id - _NEAR_EDIT_RANGE[0]) // 2
        ) * 2
        base_target = _multilingual_pair(seed, base_id, 902)[1] + (
            f" #near{base_id}"
        )
        if record_id == base_id:
            return base_target
        return _one_char_edit(base_target)
    return _multilingual_pair(seed, record_id, 1001)[1] + f" (#{record_id})"


def _record_for(seed: int, record_id: int) -> "BenchmarkRecord":
    source_raw = _source_for(seed, record_id)
    target_raw = _target_for(seed, record_id)
    speaker_raw: str | None = None
    context_prev_raw: str | None = None
    context_next_raw: str | None = None
    if _CONTEXT_RANGE[0] <= record_id <= _CONTEXT_RANGE[1]:
        speaker_raw = f"speaker-{(record_id % 9) + 1}"
        context_prev_raw = f"Previous utterance {record_id}."
        context_next_raw = f"Next utterance {record_id}."
    return BenchmarkRecord(
        record_id=record_id,
        source_raw=source_raw,
        target_raw=target_raw,
        language=_language_for(seed, record_id),
        speaker_raw=speaker_raw,
        context_prev_raw=context_prev_raw,
        context_next_raw=context_next_raw,
        file_source=None,
        provenance=(("origin", TM_BENCHMARK_CORPUS_VERSION),),
        origin_batch_id=TM_BENCHMARK_CORPUS_VERSION,
        origin_ordinal=record_id - 1,
        legacy_line_no=None,
    )


@dataclass(frozen=True)
class BenchmarkRecord:
    """One immutable, privately snapshot corpus record with stable identity."""

    record_id: int
    source_raw: str
    target_raw: str
    language: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    file_source: str | None
    provenance: tuple[tuple[str, str], ...]
    origin_batch_id: str
    origin_ordinal: int
    legacy_line_no: int | None = None

    def __post_init__(self) -> None:
        _require_int(self.record_id, "record id", minimum=1)
        for field_name, value in (
            ("source_raw", self.source_raw),
            ("target_raw", self.target_raw),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        _require_identity(self.language, "language")
        if self.language not in _LANGUAGES:
            raise ValueError(f"unknown record language: {self.language}")
        for field_name, value in (
            ("speaker_raw", self.speaker_raw),
            ("context_prev_raw", self.context_prev_raw),
            ("context_next_raw", self.context_next_raw),
            ("file_source", self.file_source),
        ):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string or None")
        for pair in self.provenance:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("provenance entries must be two-item tuples")
            _require_identity(pair[0], "provenance key")
            _require_identity(pair[1], "provenance value")
        _require_identity(self.origin_batch_id, "origin batch id")
        _require_int(self.origin_ordinal, "origin ordinal", minimum=0)
        if self.legacy_line_no is not None:
            _require_int(self.legacy_line_no, "legacy line number", minimum=1)

    def to_draft(self) -> TMRecordDraft:
        """Expose the corpus body as a frozen public store-append draft."""
        return TMRecordDraft(
            source_raw=self.source_raw,
            target_raw=self.target_raw,
            speaker_raw=self.speaker_raw,
            context_prev_raw=self.context_prev_raw,
            context_next_raw=self.context_next_raw,
            file_source=self.file_source,
            provenance=self.provenance,
        )


@dataclass(frozen=True)
class BenchmarkQuery:
    """One immutable, privately snapshot query with a stable cohort identity."""

    query_id: int
    query_raw: str
    cohort: str
    category: str
    reference_record_id: int | None

    def __post_init__(self) -> None:
        _require_int(self.query_id, "query id", minimum=1)
        if not isinstance(self.query_raw, str) or not self.query_raw:
            raise ValueError("query raw must be a non-empty string")
        if self.cohort not in _COHORTS:
            raise ValueError(f"unknown query cohort: {self.cohort}")
        if self.category not in _CATEGORIES:
            raise ValueError(f"unknown query category: {self.category}")
        if self.reference_record_id is not None:
            _require_int(
                self.reference_record_id,
                "reference record id",
                minimum=1,
            )
        if self.category == "miss" and self.reference_record_id is not None:
            raise ValueError("miss queries must not carry a reference record id")
        if (
            self.category in ("exact", "near-edit")
            and self.reference_record_id is None
        ):
            raise ValueError(
                "exact and near-edit queries require a reference record id"
            )
        if self.cohort == "exact" and self.category != "exact":
            raise ValueError("exact cohort queries must be exact category")
        if self.cohort == "fuzzy" and self.category not in (
            "near-edit",
            "miss",
        ):
            raise ValueError("fuzzy cohort queries must be near-edit or miss")


@dataclass(frozen=True)
class BenchmarkInputPlan:
    """Immutable metadata/digests for the frozen benchmark-v1 inputs.

    The plan holds no corpus materialization (no 100,000-record container);
    records and queries are consumed through the streaming iterators.
    """

    generator_version: str
    composition_version: str
    seed: int
    record_count: int
    exact_cohort_count: int
    fuzzy_cohort_count: int
    oracle_subset_record_count: int
    oracle_query_count: int
    corpus_digest: str
    corpus_composition_digest: str
    exact_cohort_digest: str
    fuzzy_cohort_digest: str
    oracle_subset_digest: str
    scorer_config_digest: str
    fast_path_config_digest: str
    fallback_path_config_digest: str
    language_counts: tuple[tuple[str, int], ...]
    cjk_count: int
    short_count: int
    duplicate_source_count: int
    multi_target_count: int
    context_count: int
    near_edit_record_pairs: int
    fuzzy_near_edit_count: int
    fuzzy_miss_count: int
    oracle_exact_query_count: int
    oracle_near_edit_query_count: int
    oracle_miss_query_count: int

    def __post_init__(self) -> None:
        _require_identity(self.generator_version, "generator version")
        _require_identity(self.composition_version, "composition version")
        _require_int(self.seed, "seed", minimum=0)
        _require_int(self.record_count, "record count", minimum=1)
        for field_name, value in (
            ("exact cohort count", self.exact_cohort_count),
            ("fuzzy cohort count", self.fuzzy_cohort_count),
            ("oracle subset record count", self.oracle_subset_record_count),
            ("oracle query count", self.oracle_query_count),
        ):
            _require_int(value, field_name, minimum=0)
        for field_name, value in (
            ("corpus digest", self.corpus_digest),
            ("corpus composition digest", self.corpus_composition_digest),
            ("exact cohort digest", self.exact_cohort_digest),
            ("fuzzy cohort digest", self.fuzzy_cohort_digest),
            ("oracle subset digest", self.oracle_subset_digest),
            ("scorer config digest", self.scorer_config_digest),
            ("fast path config digest", self.fast_path_config_digest),
            ("fallback path config digest", self.fallback_path_config_digest),
        ):
            if not isinstance(value, str) or _SHA256_DIGEST.fullmatch(value) is None:
                raise ValueError(
                    f"{field_name} must be a lowercase SHA-256 digest"
                )
        seen: set[str] = set()
        for language, count in self.language_counts:
            _require_identity(language, "language")
            _require_int(count, "language count", minimum=0)
            if language in seen:
                raise ValueError("language counts must be unique")
            seen.add(language)
        for field_name, value in (
            ("CJK count", self.cjk_count),
            ("short count", self.short_count),
            ("duplicate source count", self.duplicate_source_count),
            ("multi-target count", self.multi_target_count),
            ("context count", self.context_count),
            ("near-edit record pairs", self.near_edit_record_pairs),
            ("fuzzy near-edit count", self.fuzzy_near_edit_count),
            ("fuzzy miss count", self.fuzzy_miss_count),
            ("oracle exact query count", self.oracle_exact_query_count),
            ("oracle near-edit query count", self.oracle_near_edit_query_count),
            ("oracle miss query count", self.oracle_miss_query_count),
        ):
            _require_int(value, field_name, minimum=0)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stable_benchmark_source_digest(path: Path) -> str:
    """Hash one direct regular implementation member without aliases."""

    try:
        before = os.lstat(path)
    except OSError as error:
        raise ValueError("benchmark implementation source is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError("benchmark implementation source is not regular")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError("benchmark implementation source cannot be opened") from error
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino)
        stable = (
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != identity
            or (
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
            )
            != stable
        ):
            raise ValueError("benchmark implementation source identity changed")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        terminal = os.fstat(descriptor)
        if (
            not stat.S_ISREG(terminal.st_mode)
            or terminal.st_nlink != 1
            or (terminal.st_dev, terminal.st_ino) != identity
            or (
                terminal.st_size,
                terminal.st_mtime_ns,
                terminal.st_ctime_ns,
            )
            != stable
        ):
            raise ValueError("benchmark implementation source changed while read")
    finally:
        os.close(descriptor)
    try:
        after = os.lstat(path)
    except OSError as error:
        raise ValueError("benchmark implementation source path changed") from error
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or (after.st_dev, after.st_ino) != identity
        or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != stable
    ):
        raise ValueError("benchmark implementation source path changed")
    return digest.hexdigest()


def benchmark_implementation_fingerprint(
    repository_root: Path | None = None,
) -> str:
    """Digest one stable two-pass snapshot of the Gate D implementation.

    Each source is already read no-follow with an identity/metadata barrier.
    The second full inventory pass additionally detects a change to an
    earlier source while a later source was being read.  A mixed closure is
    never returned as a valid implementation fingerprint.
    """

    if repository_root is None:
        root = Path(__file__).resolve().parent
    else:
        if type(repository_root) is not _NATIVE_PATH_TYPE:
            raise TypeError("repository root must be an exact native Path")
        try:
            root = repository_root.resolve(strict=True)
        except OSError as error:
            raise ValueError("repository root is unavailable") from error
    try:
        root_stat = os.lstat(root)
    except OSError as error:
        raise ValueError("repository root is unavailable") from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError("repository root must be a direct directory")
    def capture() -> tuple[tuple[str, str], ...]:
        return tuple(
            (relative, _stable_benchmark_source_digest(root / relative))
            for relative in BENCHMARK_IMPLEMENTATION_SOURCE_PATHS
        )

    source_files = capture()
    if capture() != source_files:
        raise ValueError("benchmark implementation changed during snapshot")
    return hashlib.sha256(
        (
            BENCHMARK_IMPLEMENTATION_FINGERPRINT_VERSION
            + "\0"
            + _canonical_json(
                {
                    "proof_query_version": CANDIDATE_PROOF_QUERY_VERSION,
                    "source_files": [list(item) for item in source_files],
                }
            )
        ).encode("utf-8")
    ).hexdigest()


def benchmark_digest(
    generator_version: str,
    kind: str,
    items: Iterable[Mapping[str, object]],
) -> str:
    """Compute the canonical, versioned, closed digest of ordered items."""
    hasher = hashlib.sha256()
    header = _canonical_json(
        {
            "digest_schema": TM_BENCHMARK_DIGEST_SCHEMA,
            "kind": kind,
            "generator_version": generator_version,
        }
    )
    hasher.update(header.encode("utf-8"))
    hasher.update(b"\n")
    for item in items:
        hasher.update(_canonical_json(item).encode("utf-8"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def _record_payload(record: BenchmarkRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "source_raw": record.source_raw,
        "target_raw": record.target_raw,
        "language": record.language,
        "speaker_raw": record.speaker_raw,
        "context_prev_raw": record.context_prev_raw,
        "context_next_raw": record.context_next_raw,
        "file_source": record.file_source,
        "legacy_line_no": record.legacy_line_no,
        "origin_batch_id": record.origin_batch_id,
        "origin_ordinal": record.origin_ordinal,
        "provenance": [
            [key, value] for key, value in record.provenance
        ],
    }


def _query_payload(query: BenchmarkQuery) -> dict[str, object]:
    return {
        "query_id": query.query_id,
        "query_raw": query.query_raw,
        "cohort": query.cohort,
        "category": query.category,
        "reference_record_id": query.reference_record_id,
    }


def iter_corpus_records(
    *,
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
) -> Iterator[BenchmarkRecord]:
    """Stream the deterministic corpus in ascending, unique record id order."""
    _validate_generation_params(seed, record_count)
    for record_id in range(1, record_count + 1):
        record = _record_for(seed, record_id)
        if (
            _is_near_edit_base(record_id)
            and record_id + 1 <= record_count
        ):
            edited_source = _source_for(seed, record_id + 1)
            if edited_source != _one_char_edit(record.source_raw):
                raise RuntimeError(
                    "near-edit corpus pair invariant violated"
                )
        yield record


def iter_exact_queries(
    *,
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
    cohort_count: int = TM_BENCHMARK_EXACT_COHORT_COUNT,
) -> Iterator[BenchmarkQuery]:
    """Stream the frozen exact cohort; every query equals a record source."""
    _validate_generation_params(seed, record_count)
    _require_int(cohort_count, "exact cohort count", minimum=0)
    for index in range(1, cohort_count + 1):
        reference_record_id = (index * 83) % record_count + 1
        yield BenchmarkQuery(
            query_id=index,
            query_raw=_source_for(seed, reference_record_id),
            cohort="exact",
            category="exact",
            reference_record_id=reference_record_id,
        )


def iter_fuzzy_queries(
    *,
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
    cohort_count: int = TM_BENCHMARK_FUZZY_COHORT_COUNT,
) -> Iterator[BenchmarkQuery]:
    """Stream the frozen fuzzy cohort: near-edit then miss queries."""
    _validate_generation_params(seed, record_count)
    _require_int(cohort_count, "fuzzy cohort count", minimum=0)
    near_edit_count = cohort_count * 5 // 6
    for index in range(1, near_edit_count + 1):
        reference_record_id = (index * 83) % record_count + 1
        yield BenchmarkQuery(
            query_id=index,
            query_raw=_one_char_edit(
                _source_for(seed, reference_record_id)
            ),
            cohort="fuzzy",
            category="near-edit",
            reference_record_id=reference_record_id,
        )
    for index in range(near_edit_count + 1, cohort_count + 1):
        miss_index = index - near_edit_count
        query_raw = (
            f"{TM_BENCHMARK_MISS_PREFIX}-{miss_index:04d}-"
            f"{_derive(seed, 505, miss_index) & 0xFFFF:04x}"
        )
        yield BenchmarkQuery(
            query_id=index,
            query_raw=query_raw,
            cohort="fuzzy",
            category="miss",
            reference_record_id=None,
        )


def _oracle_subset_ids(
    seed: int,
    record_count: int,
    subset_count: int,
) -> tuple[int, ...]:
    _require_int(subset_count, "oracle subset record count", minimum=0)
    if subset_count > record_count:
        raise ValueError(
            "oracle subset record count must not exceed corpus record count"
        )
    return tuple(
        sorted(
            {
                (index * 17) % record_count + 1
                for index in range(1, subset_count + 1)
            }
        )
    )


def _oracle_near_edit_reference(
    seed: int,
    oracle_ids: tuple[int, ...],
    index: int,
) -> int:
    if not oracle_ids:
        raise ValueError("oracle near-edit queries need an oracle subset")
    found = 0
    step = 0
    while True:
        candidate = oracle_ids[(step * 13) % len(oracle_ids)]
        if len(_source_for(seed, candidate)) >= 2:
            found += 1
            if found == index:
                return candidate
        step += 1


def iter_oracle_subset_records(
    *,
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
    subset_count: int = TM_BENCHMARK_ORACLE_SUBSET_RECORD_COUNT,
) -> Iterator[BenchmarkRecord]:
    """Stream the fixed oracle record subset in ascending id order."""
    _validate_generation_params(seed, record_count)
    for record_id in _oracle_subset_ids(seed, record_count, subset_count):
        yield _record_for(seed, record_id)


def iter_oracle_queries(
    *,
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
    subset_count: int = TM_BENCHMARK_ORACLE_SUBSET_RECORD_COUNT,
    query_count: int = TM_BENCHMARK_ORACLE_QUERY_COUNT,
) -> Iterator[BenchmarkQuery]:
    """Stream the fixed 200-query oracle cohort (160 exact / 20 near-edit / 20 miss)."""
    _validate_generation_params(seed, record_count)
    _require_int(query_count, "oracle query count", minimum=0)
    oracle_ids = _oracle_subset_ids(seed, record_count, subset_count)
    exact_count = query_count * 4 // 5
    near_edit_count = query_count // 10
    for index in range(1, exact_count + 1):
        reference_record_id = oracle_ids[(index * 31) % len(oracle_ids)]
        yield BenchmarkQuery(
            query_id=index,
            query_raw=_source_for(seed, reference_record_id),
            cohort="oracle",
            category="exact",
            reference_record_id=reference_record_id,
        )
    for index in range(exact_count + 1, exact_count + near_edit_count + 1):
        reference_record_id = _oracle_near_edit_reference(
            seed,
            oracle_ids,
            index - exact_count,
        )
        yield BenchmarkQuery(
            query_id=index,
            query_raw=_one_char_edit(
                _source_for(seed, reference_record_id)
            ),
            cohort="oracle",
            category="near-edit",
            reference_record_id=reference_record_id,
        )
    for index in range(
        exact_count + near_edit_count + 1,
        query_count + 1,
    ):
        miss_index = index - exact_count - near_edit_count
        query_raw = (
            f"{TM_BENCHMARK_MISS_PREFIX}-o{miss_index:04d}-"
            f"{_derive(seed, 606, miss_index) & 0xFFFF:04x}"
        )
        yield BenchmarkQuery(
            query_id=index,
            query_raw=query_raw,
            cohort="oracle",
            category="miss",
            reference_record_id=None,
        )


def _near_edit_pair_count(record_count: int) -> int:
    if record_count < _NEAR_EDIT_RANGE[0]:
        return 0
    available = min(record_count, _NEAR_EDIT_RANGE[1]) - _NEAR_EDIT_RANGE[0] + 1
    return available // 2


def _scan_corpus(
    seed: int,
    record_count: int,
    generator_version: str,
) -> tuple[str, dict[str, object]]:
    """One streaming pass: corpus digest plus composition facts from facts."""
    hasher = hashlib.sha256()
    hasher.update(
        _canonical_json(
            {
                "digest_schema": TM_BENCHMARK_DIGEST_SCHEMA,
                "kind": "corpus",
                "generator_version": generator_version,
            }
        ).encode("utf-8")
    )
    hasher.update(b"\n")
    language_counts: dict[str, int] = {}
    cjk_count = 0
    short_count = 0
    context_count = 0
    source_groups: dict[str, tuple[int, set[str]]] = {}
    for record in iter_corpus_records(seed=seed, record_count=record_count):
        hasher.update(
            _canonical_json(_record_payload(record)).encode("utf-8")
        )
        hasher.update(b"\n")
        language = record.language
        language_counts[language] = language_counts.get(language, 0) + 1
        if _contains_cjk(record.source_raw) or _contains_cjk(
            record.target_raw
        ):
            cjk_count += 1
        if (
            len(record.source_raw) <= _SHORT_MAX_LENGTH
            and len(record.target_raw) <= _SHORT_MAX_LENGTH
        ):
            short_count += 1
        if (
            record.speaker_raw is not None
            or record.context_prev_raw is not None
            or record.context_next_raw is not None
        ):
            context_count += 1
        entry = source_groups.get(record.source_raw)
        if entry is None:
            source_groups[record.source_raw] = (1, {record.target_raw})
        else:
            count_in_group, targets = entry
            merged_targets = set(targets)
            merged_targets.add(record.target_raw)
            source_groups[record.source_raw] = (
                count_in_group + 1,
                merged_targets,
            )
    duplicate_source_count = 0
    multi_target_count = 0
    for record_count_in_group, targets in source_groups.values():
        if record_count_in_group >= 2:
            duplicate_source_count += record_count_in_group
        if len(targets) >= 2:
            multi_target_count += record_count_in_group
    composition: dict[str, object] = {
        "composition_version": TM_BENCHMARK_COMPOSITION_VERSION,
        "generator_version": generator_version,
        "seed": seed,
        "record_count": record_count,
        "language_counts": dict(sorted(language_counts.items())),
        "cjk_count": cjk_count,
        "short_count": short_count,
        "duplicate_source_count": duplicate_source_count,
        "multi_target_count": multi_target_count,
        "context_count": context_count,
        "near_edit_record_pairs": _near_edit_pair_count(record_count),
    }
    return hasher.hexdigest(), composition


def _contains_cjk(text: str) -> bool:
    return any(
        "\u3400" <= char <= "\u4DBF"
        or "\u4E00" <= char <= "\u9FFF"
        or "\u3040" <= char <= "\u30FF"
        or "\uAC00" <= char <= "\uD7AF"
        or "\uF900" <= char <= "\uFAFF"
        for char in text
    )


def _cohort_queries_payloads(
    queries: Iterable[BenchmarkQuery],
) -> list[dict[str, object]]:
    return [_query_payload(query) for query in queries]


def _oracle_subset_digest(
    generator_version: str,
    oracle_queries: list[BenchmarkQuery],
    oracle_records: list[BenchmarkRecord],
) -> str:
    items: list[dict[str, object]] = [
        {"section": "record", **_record_payload(record)}
        for record in oracle_records
    ]
    items.extend(
        {"section": "query", **_query_payload(query)}
        for query in oracle_queries
    )
    return benchmark_digest(generator_version, "oracle-subset", items)


def _as_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer")
    return value


def _as_language_counts(
    value: object,
) -> tuple[tuple[str, int], ...]:
    if not isinstance(value, dict):
        raise RuntimeError("language counts must be a mapping")
    result: list[tuple[str, int]] = []
    for language, count in value.items():
        if not isinstance(language, str):
            raise RuntimeError("language name must be a string")
        result.append((language, _as_int(count, "language count")))
    return tuple(result)


def compute_benchmark_input_plan(
    *,
    seed: int = TM_BENCHMARK_DEFAULT_SEED,
    record_count: int = TM_BENCHMARK_CORPUS_RECORD_COUNT,
    generator_version: str = TM_BENCHMARK_CORPUS_VERSION,
    composition_version: str = TM_BENCHMARK_COMPOSITION_VERSION,
    exact_cohort_count: int = TM_BENCHMARK_EXACT_COHORT_COUNT,
    fuzzy_cohort_count: int = TM_BENCHMARK_FUZZY_COHORT_COUNT,
    oracle_subset_record_count: int = TM_BENCHMARK_ORACLE_SUBSET_RECORD_COUNT,
    oracle_query_count: int = TM_BENCHMARK_ORACLE_QUERY_COUNT,
) -> BenchmarkInputPlan:
    """Compute the full frozen input plan (digests and facts) deterministically."""
    _validate_generation_params(seed, record_count)
    _require_identity(generator_version, "generator version")
    _require_identity(composition_version, "composition version")
    _require_int(exact_cohort_count, "exact cohort count", minimum=0)
    _require_int(fuzzy_cohort_count, "fuzzy cohort count", minimum=0)
    _require_int(oracle_query_count, "oracle query count", minimum=0)
    if oracle_subset_record_count > record_count:
        raise ValueError(
            "oracle subset record count must not exceed corpus record count"
        )

    corpus_digest, composition = _scan_corpus(
        seed,
        record_count,
        generator_version,
    )
    composition["exact_cohort_count"] = exact_cohort_count
    composition["fuzzy_cohort_count"] = fuzzy_cohort_count
    composition["fuzzy_near_edit_count"] = fuzzy_cohort_count * 5 // 6
    composition["fuzzy_miss_count"] = (
        fuzzy_cohort_count - fuzzy_cohort_count * 5 // 6
    )
    composition["oracle_subset_record_count"] = oracle_subset_record_count
    composition["oracle_query_count"] = oracle_query_count
    composition["oracle_exact_query_count"] = oracle_query_count * 4 // 5
    composition["oracle_near_edit_query_count"] = oracle_query_count // 10
    composition["oracle_miss_query_count"] = (
        oracle_query_count
        - oracle_query_count * 4 // 5
        - oracle_query_count // 10
    )
    composition_digest = benchmark_digest(
        generator_version,
        "corpus-composition",
        [composition],
    )

    exact_queries = list(
        iter_exact_queries(
            seed=seed,
            record_count=record_count,
            cohort_count=exact_cohort_count,
        )
    )
    exact_cohort_digest = benchmark_digest(
        generator_version,
        "exact-cohort",
        _cohort_queries_payloads(exact_queries),
    )

    fuzzy_queries = list(
        iter_fuzzy_queries(
            seed=seed,
            record_count=record_count,
            cohort_count=fuzzy_cohort_count,
        )
    )
    fuzzy_cohort_digest = benchmark_digest(
        generator_version,
        "fuzzy-cohort",
        _cohort_queries_payloads(fuzzy_queries),
    )

    oracle_records = list(
        iter_oracle_subset_records(
            seed=seed,
            record_count=record_count,
            subset_count=oracle_subset_record_count,
        )
    )
    oracle_queries = list(
        iter_oracle_queries(
            seed=seed,
            record_count=record_count,
            subset_count=oracle_subset_record_count,
            query_count=oracle_query_count,
        )
    )
    oracle_subset_digest = _oracle_subset_digest(
        generator_version,
        oracle_queries,
        oracle_records,
    )

    scorer_config_digest = benchmark_digest(
        generator_version,
        "scorer-config",
        [
            {
                "scorer_config_version": TM_BENCHMARK_SCORER_CONFIG_VERSION,
                "scorer_version": SCORER_VERSION_V1,
                "minimum_similarity": TM_BENCHMARK_MINIMUM_SIMILARITY,
                "top_k": TM_BENCHMARK_TOP_K,
                "candidate_budget_version": CANDIDATE_BUDGET_VERSION,
            }
        ],
    )
    fast_path_config_digest = benchmark_digest(
        generator_version,
        "path-config",
        [
            {
                "path_config_version": TM_BENCHMARK_PATH_CONFIG_VERSION,
                "execution_path": "FTS5_TRIGRAM",
                "index_kind": "FTS5_TRIGRAM",
            }
        ],
    )
    fallback_path_config_digest = benchmark_digest(
        generator_version,
        "path-config",
        [
            {
                "path_config_version": TM_BENCHMARK_PATH_CONFIG_VERSION,
                "execution_path": "GRAM_FALLBACK",
                "gram_sizes": [1, 2, 3],
            }
        ],
    )

    language_counts = _as_language_counts(composition["language_counts"])

    return BenchmarkInputPlan(
        generator_version=generator_version,
        composition_version=composition_version,
        seed=seed,
        record_count=record_count,
        exact_cohort_count=exact_cohort_count,
        fuzzy_cohort_count=fuzzy_cohort_count,
        oracle_subset_record_count=oracle_subset_record_count,
        oracle_query_count=oracle_query_count,
        corpus_digest=corpus_digest,
        corpus_composition_digest=composition_digest,
        exact_cohort_digest=exact_cohort_digest,
        fuzzy_cohort_digest=fuzzy_cohort_digest,
        oracle_subset_digest=oracle_subset_digest,
        scorer_config_digest=scorer_config_digest,
        fast_path_config_digest=fast_path_config_digest,
        fallback_path_config_digest=fallback_path_config_digest,
        language_counts=language_counts,
        cjk_count=_as_int(composition["cjk_count"], "cjk_count"),
        short_count=_as_int(composition["short_count"], "short_count"),
        duplicate_source_count=_as_int(composition["duplicate_source_count"], "duplicate_source_count"),
        multi_target_count=_as_int(composition["multi_target_count"], "multi_target_count"),
        context_count=_as_int(composition["context_count"], "context_count"),
        near_edit_record_pairs=_as_int(composition["near_edit_record_pairs"], "near_edit_record_pairs"),
        fuzzy_near_edit_count=_as_int(composition["fuzzy_near_edit_count"], "fuzzy_near_edit_count"),
        fuzzy_miss_count=_as_int(composition["fuzzy_miss_count"], "fuzzy_miss_count"),
        oracle_exact_query_count=_as_int(composition["oracle_exact_query_count"], "oracle_exact_query_count"),
        oracle_near_edit_query_count=_as_int(
            composition["oracle_near_edit_query_count"],
            "oracle_near_edit_query_count",
        ),
        oracle_miss_query_count=_as_int(composition["oracle_miss_query_count"], "oracle_miss_query_count"),
    )


def compute_benchmark_contract() -> BenchmarkContract:
    """Build the frozen BenchmarkContract bound to this generator's digests."""
    plan = compute_benchmark_input_plan()
    return BenchmarkContract(
        contract_version=BENCHMARK_CONTRACT_VERSION,
        corpus_generator_version=plan.generator_version,
        corpus_seed=plan.seed,
        corpus_record_count=plan.record_count,
        corpus_digest=plan.corpus_digest,
        corpus_composition_version=plan.composition_version,
        corpus_composition_digest=plan.corpus_composition_digest,
        exact_cohort_digest=plan.exact_cohort_digest,
        exact_min_samples=TM_BENCHMARK_EXACT_MIN_SAMPLES,
        exact_cohort_count=plan.exact_cohort_count,
        fuzzy_cohort_digest=plan.fuzzy_cohort_digest,
        fuzzy_min_samples=TM_BENCHMARK_FUZZY_MIN_SAMPLES,
        fuzzy_cohort_count=plan.fuzzy_cohort_count,
        oracle_subset_digest=plan.oracle_subset_digest,
        oracle_subset_record_count=plan.oracle_subset_record_count,
        oracle_query_count=plan.oracle_query_count,
        top_k=TM_BENCHMARK_TOP_K,
        minimum_similarity=TM_BENCHMARK_MINIMUM_SIMILARITY,
        warmup_queries_per_cohort=TM_BENCHMARK_WARMUP_QUERIES_PER_COHORT,
        measured_repeats=TM_BENCHMARK_MEASURED_REPEATS,
        percentile_method=BENCHMARK_PERCENTILE_METHOD,
        rss_scope=BENCHMARK_RSS_SCOPE,
        candidate_budget_version=CANDIDATE_BUDGET_VERSION,
        scorer_config_digest=plan.scorer_config_digest,
        fast_path_config_digest=plan.fast_path_config_digest,
        fallback_path_config_digest=plan.fallback_path_config_digest,
        exact_p95_gate_ms=TM_BENCHMARK_EXACT_P95_GATE_MS,
        fuzzy_p95_gate_ms=TM_BENCHMARK_FUZZY_P95_GATE_MS,
        migration_gate_seconds=TM_BENCHMARK_MIGRATION_GATE_SECONDS,
        peak_rss_gate_mib=TM_BENCHMARK_PEAK_RSS_GATE_MIB,
        candidate_recall_gate=TM_BENCHMARK_CANDIDATE_RECALL_GATE,
    )


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in contract: {key}")
        result[key] = value
    return result


def load_benchmark_contract(path: Path) -> BenchmarkContract:
    """Strictly load the committed contract before any generation runs.

    Rejects duplicate JSON keys, non-finite numbers, non-object roots, and any
    unknown/missing/mistyped payload field through the frozen contract codec.
    """
    if not isinstance(path, Path):
        raise TypeError("contract path must be a Path")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            "cannot read committed benchmark contract file"
        ) from exc

    def reject_non_finite(value: str) -> None:
        raise ValueError(
            f"non-finite JSON number is not allowed: {value}"
        )

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except (json.JSONDecodeError, TypeError):
        raise ValueError(
            "committed benchmark contract is not valid strict JSON"
        ) from None
    if not isinstance(parsed, dict):
        raise ValueError("committed benchmark contract must be a JSON object")
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    contract = contract_from_json(canonical)
    if not isinstance(contract, BenchmarkContract):
        raise ValueError(
            "committed benchmark contract is not a BenchmarkContract"
        )
    return contract


def recompute_benchmark_inputs(
    contract_path: Path,
) -> BenchmarkInputPlan:
    """Recompute every input digest and fail closed on any contract mismatch.

    The committed contract is loaded strictly (never trusting its digest
    values as truth), the full plan is recomputed from the frozen generator
    parameters, and every digest/count is compared.  Any difference raises.
    """
    contract = load_benchmark_contract(contract_path)
    plan = compute_benchmark_input_plan()
    checks = (
        (
            "corpus generator version",
            contract.corpus_generator_version,
            plan.generator_version,
        ),
        ("corpus seed", contract.corpus_seed, plan.seed),
        (
            "corpus record count",
            contract.corpus_record_count,
            plan.record_count,
        ),
        (
            "corpus composition version",
            contract.corpus_composition_version,
            plan.composition_version,
        ),
        ("corpus digest", contract.corpus_digest, plan.corpus_digest),
        (
            "corpus composition digest",
            contract.corpus_composition_digest,
            plan.corpus_composition_digest,
        ),
        (
            "exact cohort digest",
            contract.exact_cohort_digest,
            plan.exact_cohort_digest,
        ),
        (
            "exact cohort count",
            contract.exact_cohort_count,
            plan.exact_cohort_count,
        ),
        (
            "fuzzy cohort digest",
            contract.fuzzy_cohort_digest,
            plan.fuzzy_cohort_digest,
        ),
        (
            "fuzzy cohort count",
            contract.fuzzy_cohort_count,
            plan.fuzzy_cohort_count,
        ),
        (
            "oracle subset digest",
            contract.oracle_subset_digest,
            plan.oracle_subset_digest,
        ),
        (
            "oracle subset record count",
            contract.oracle_subset_record_count,
            plan.oracle_subset_record_count,
        ),
        (
            "oracle query count",
            contract.oracle_query_count,
            plan.oracle_query_count,
        ),
        ("top_k", contract.top_k, TM_BENCHMARK_TOP_K),
        (
            "minimum similarity",
            contract.minimum_similarity,
            TM_BENCHMARK_MINIMUM_SIMILARITY,
        ),
        (
            "scorer config digest",
            contract.scorer_config_digest,
            plan.scorer_config_digest,
        ),
        (
            "fast path config digest",
            contract.fast_path_config_digest,
            plan.fast_path_config_digest,
        ),
        (
            "fallback path config digest",
            contract.fallback_path_config_digest,
            plan.fallback_path_config_digest,
        ),
    )
    for field_name, observed, expected in checks:
        if observed != expected:
            raise ValueError(
                "committed benchmark contract "
                f"{field_name} does not match recomputation"
            )
    return plan


__all__ = [
    "BENCHMARK_IMPLEMENTATION_FINGERPRINT_VERSION",
    "BENCHMARK_IMPLEMENTATION_SOURCE_PATHS",
    "TM_BENCHMARK_CANDIDATE_RECALL_GATE",
    "TM_BENCHMARK_COMPOSITION_VERSION",
    "TM_BENCHMARK_CORPUS_RECORD_COUNT",
    "TM_BENCHMARK_CORPUS_VERSION",
    "TM_BENCHMARK_DEFAULT_SEED",
    "TM_BENCHMARK_DIGEST_SCHEMA",
    "TM_BENCHMARK_EXACT_COHORT_COUNT",
    "TM_BENCHMARK_EXACT_MIN_SAMPLES",
    "TM_BENCHMARK_EXACT_P95_GATE_MS",
    "TM_BENCHMARK_FUZZY_COHORT_COUNT",
    "TM_BENCHMARK_FUZZY_MIN_SAMPLES",
    "TM_BENCHMARK_FUZZY_P95_GATE_MS",
    "TM_BENCHMARK_MEASURED_REPEATS",
    "TM_BENCHMARK_MIGRATION_GATE_SECONDS",
    "TM_BENCHMARK_MINIMUM_SIMILARITY",
    "TM_BENCHMARK_MISS_PREFIX",
    "TM_BENCHMARK_ORACLE_QUERY_COUNT",
    "TM_BENCHMARK_ORACLE_SUBSET_RECORD_COUNT",
    "TM_BENCHMARK_PATH_CONFIG_VERSION",
    "TM_BENCHMARK_PEAK_RSS_GATE_MIB",
    "TM_BENCHMARK_SCORER_CONFIG_VERSION",
    "TM_BENCHMARK_TOP_K",
    "TM_BENCHMARK_WARMUP_QUERIES_PER_COHORT",
    "BenchmarkInputPlan",
    "BenchmarkQuery",
    "BenchmarkRecord",
    "benchmark_digest",
    "benchmark_implementation_fingerprint",
    "compute_benchmark_contract",
    "compute_benchmark_input_plan",
    "iter_corpus_records",
    "iter_exact_queries",
    "iter_fuzzy_queries",
    "iter_oracle_queries",
    "iter_oracle_subset_records",
    "load_benchmark_contract",
    "recompute_benchmark_inputs",
]
