# 设计文档

## 概述

Feature 5 把当前内存 JSONL exact engine 演进为每资源隔离、可迁移、可解释的本地 TM 检索子系统。迁移并原子激活后，同目录 canonical SQLite sidecar 成为该资源唯一的运行时读写权威，保存多译文、raw context、provenance 和完整候选索引；原 JSONL 被登记为只读历史快照，只用于兜底恢复、显式导入/导出、外部互操作和审计，不再参与正常查询或保存。检索严格保持 raw exact 兼容 winner，再返回有明确证据的 context 和显式评分的 fuzzy 建议。

候选召回与最终 CAT 相似度完全分离。FTS5 trigram 或自有 n-gram postings 只缩小候选集合；每个 fuzzy 候选都运行 Levenshtein 与 multiset character-bigram Dice，`scorer-v1` 以二者等权平均作为 final similarity。Core 同时发布版本化 `TextMatcher`，供 Qt 项目搜索和术语产品消费，但不拥有任何 UI、工作区或 Parser 行为。

### 目标

- 证明旧 raw exact、last-valid winner、Active/Lookup/Update 与 Excel 三态等价。
- 安全迁移并保留多译文、context、provenance、原始 JSONL 字节和可核对的来源绑定。
- 提供确定性的 `EXACT → CONTEXT → FUZZY` query pipeline 和双 source evidence。
- 提供 Unicode/CJK 可版本化文本匹配，以及 100k TM 性能门。

### 非目标

- Qt 控件、导航、高亮、偏好或 suggestion card。
- Parser/Codec、项目文件、TMX vendor context mapping。
- 云端、账号、共享锁、向量/语义模型和机器翻译。
- 在旧 Excel 三态中暴露 context/fuzzy 第四状态。

## Boundary Commitments

### This Spec Owns

- `TMRecord`、query/result/evidence/migration/export/search frozen contracts。
- 每 TM 资源独立 SQLite schema、事务、索引、schema upgrade 与 backup。
- JSONL preflight、来源快照绑定、旁路迁移、idempotent retry、compatibility export。
- raw exact、positive-context classification、candidate recall、Levenshtein/Dice 与稳定排序。
- `TextMatcher text-v1` 的 case-fold projection、UAX #29 word boundary、pure CJK tailoring。
- `TextMatcher` 的三态 capability、验证证据解析、用途门控与同一快照结果信封。
- 旧 `TMEngine.query_exact/save_record` 的 compatibility facade 与非 Qt benchmark。

### Out of Boundary

- `EditorController` 的 Qt-facing suggestion adaptation 和 apply/navigation。
- Resource Settings UI、资源路径解释、Match Case/Whole Word 控件。
- 项目/格式 parser 与 TMX context interchange。
- speaker alias/avatar 和其他展示 profile。
- 网络、协作与跨端同步。

### Allowed Dependencies

- Python 3.14 标准库：`sqlite3`、`json`、`hashlib`、`unicodedata`、`multiprocessing`、`pathlib`。
- SQLite FTS5 是可探测 fast-path capability，不是正确性唯一依赖。
- Core 可以读取中立 raw records，不得依赖 PySide6、xlwings、Controller、workspace state 或 Parser 实现。
- Compatibility facade 可依赖新 Core ports；新 Core 不反向依赖旧 `TMEngine`。

### Revalidation Triggers

- Python/SQLite/UCD 版本、FTS5 capability 或 WAL 安全范围改变。
- JSONL source/target/metadata 字段、last-write 兼容语义或 ResourceConfig 路径语义改变。
- scorer weights、normalization、candidate index、threshold 默认值或 stable tie key 改变。
- `TextMatcher` semantics version、验证 cohort/fixture/evaluator/artifact digest、word-break data 或 offset contract 改变。
- JSONL snapshot binding schema、显式 import/rebuild/export 行为或 `SOURCE_DIVERGED` 状态转换改变。
- Qt adapter、Parser record adapter 或 TMX interchange 开始消费 canonical context。

## 架构

### 现有架构分析

- `TMEngine` eager-load JSONL，并用 raw source dict 实现 exact；重复 source 后写胜出。
- `LogicController` 只允许 exact TM → terms → no match 三态。
- `EditorController` 按 Active+Lookup 聚合资源，按 Active+Update 写入；单资源失败不得污染其他资源。
- 当前 importer 原子替换 JSONL，但会折叠同 source 且不保留 context。
- 现有 benchmark 只测三条默认 TM 的 Excel 路径，不足以证明 100k fuzzy。

### 架构模式与边界图

```mermaid
graph LR
    Contracts[TM contracts] --> Store[SQLite TM store]
    Contracts --> ActivationDurability[Activation journal and recovery]
    Store --> Coordinator[Resource store coordinator]
    ActivationDurability --> Coordinator
    Contracts --> Migration[JSONL migration]
    Contracts --> MatcherCore[Text matcher algorithm]
    MatcherEvidence[Matcher validation evidence] --> MatcherGate[Matcher capability provider]
    MatcherCore --> MatcherGate
    Coordinator --> Candidate[Candidate retriever]
    Similarity[Similarity scorers] --> Retrieval[TM retrieval]
    Candidate --> Retrieval
    Coordinator --> Retrieval
    Migration --> Coordinator
    Retrieval --> Facade[Legacy TM facade]
    Coordinator --> Facade
    Benchmark[TM benchmark] --> FuzzyGate[Fuzzy capability gate]
    Retrieval --> FuzzyGate
    FuzzyGate --> Facade
    MatcherGate --> ProductAdapters[Qt independent product adapters]
```

依赖方向为 Contracts → Store/Activation Durability/Migration/Matcher/Scorers → Coordinator/Retrieval/Capability Gates → Compatibility Facade。Activation Durability 通过窄 store-validation port 服务 coordinator，不反向依赖 `SQLiteTMStore` 具体实现；候选索引没有返回 final similarity 的权力。physical canonical activation、fuzzy benchmark 和 matcher capability 是三个独立状态机，任何一项都不得替另一项宣称就绪。

### 技术栈

| 层 | 选择 / 版本 | 作用 | 说明 |
|----|-------------|------|------|
| Runtime | Python 3.14 | 类型、迁移、评分、benchmark | 标准库 |
| Storage | SQLite 3.x via `sqlite3` | canonical per-resource TM | 首版 rollback journal |
| Candidate | FTS5 trigram + gram postings | recall-only | capability/fallback |
| Unicode | pinned UAX #29 / Unicode 16.0.0 data | text-v1 word boundary/script | 无运行时网络 |
| Compatibility | Python facade | 旧 exact/save 接缝 | Excel 三态不变 |

当前实测 SQLite 3.51.2 位于 WAL-reset advisory 影响范围，因此 `journal_mode=DELETE` 是设计要求而非性能建议。

## File Structure Plan

### Directory Structure

```text
/
├── tm_contracts.py                  # Core frozen contracts、Enums、Protocols
├── tm_sqlite_store.py               # per-resource coordinator facade、schema、CRUD、source binding
├── tm_activation_journal.py         # activation journal/terminal codec 与 durable file protocol
├── tm_activation_recovery.py        # phase recovery、成套 publication/rollback 与窄 store-validation port
├── tm_candidate_index.py            # FTS5/gram candidate retrievers
├── tm_similarity.py                 # Levenshtein、Dice、scorer-v1
├── tm_retrieval.py                  # exact/context/fuzzy pipeline 与聚合
├── tm_migration.py                  # JSONL preflight/migrate/export/upgrade
├── text_matcher.py                  # text-v1 纯算法、fold projection 与 hit logic
├── matcher_capability.py             # evidence evaluator、三态发布与 gated port
├── unicode_word_break_data.py       # generated pinned property tables
├── tm_engine.py                     # 激活 gate 后的 compatibility facade
├── resource_importer.py             # 已激活资源调用 canonical import port
├── tm_benchmark.py                  # 100k corpus、latency、RSS、recall
├── benchmark_tm_contract.json       # thresholds、corpus/scorer/index config
└── tests/
    ├── assets/
    │   ├── tm_migration_cases.jsonl
    │   ├── tm_similarity_vectors.json
    │   ├── text_matcher_vectors.json
    │   └── matcher_validation_manifest.json
    ├── test_tm_contracts.py
    ├── test_tm_sqlite_store.py
    ├── test_tm_candidate_index.py
    ├── test_tm_similarity.py
    ├── test_tm_retrieval.py
    ├── test_tm_migration.py
    ├── test_text_matcher.py
    ├── test_matcher_capability.py
    ├── test_tm_engine_compat.py
    └── test_tm_benchmark_contract.py
```

Activation 模块在 Task 5.9 闭合完整恢复矩阵后、Cluster D 统一复审前做行为保持型提取。`tm_sqlite_store.py` 在 Feature 5 内继续保持既有 `ResourceStoreCoordinator` 导入入口，但不再拥有 journal/terminal canonical codec、exclusive temporary/replace/fsync 原语或逐 phase 恢复/回滚实现；新模块不得反向导入 `SQLiteTMStore`，只能消费 frozen contracts 与显式窄端口。提取不得修改 journal phase、错误码、token/nonce 单次语义、fault-injection 顺序或 public lease/activation 行为；原 Cluster D characterization/failure matrix 必须在移动前后使用同一断言通过。`tm_contracts.py` 与 `tm_stage_sealer.py` 不属于本次提取范围，待 Feature 5 契约面稳定后另行评估。

### Modified Files

- `tm_engine.py` — 仅在 migration/exact parity gate 后适配新 facade；公共 exact/save 形状保持。
- `resource_importer.py` — 本规格内接入 canonical import port；已激活资源不再先改 JSONL 或自行折叠 canonical variants。
- `tests/test_excel_adapter_contract.py` — 增加 sidecar 激活后的三态 parity。
- `backend_throughput_harness.py` 不扩展为 fuzzy harness；保留旧路径基线。

## 系统流程

### JSONL migration

```mermaid
flowchart TD
    Source[Original JSONL] --> Preflight[Read only preflight]
    Preflight -->|invalid fatal| Report[Failure report]
    Preflight --> Stage[Build mutable staged database]
    Stage --> Index[Build all candidate indexes]
    Index --> Validate[Integrity FK counts exact parity source binding]
    Validate -->|fail| Quarantine[Quarantine staged database]
    Validate -->|pass| Close[Close connections and fsync]
    Close --> Seal[Create immutable SealedStage]
    Seal --> Drain[Coordinator drains operation leases]
    Drain --> Activate[Backup replace fsync reopen]
    Activate -->|health pass| Canonical[Publish one canonical generation]
    Canonical --> Exact[Enable exact read and save facade]
    Canonical --> ContextGate{context correctness passed?}
    ContextGate -->|yes| Context[Enable context classification]
    Canonical --> FuzzyGate{benchmark-v1 passed?}
    FuzzyGate -->|yes| Fuzzy[Enable fuzzy capability]
    FuzzyGate -->|no| Exact
    Activate -->|health fail| Restore[Restore last known good]
    Report --> Source
    Quarantine --> Source
    Restore --> Source
```

原 JSONL 在所有迁移路径中保持不变。重复相同 SHA-256 migration 通过 completed `tm_origin_batch(kind='migration')` 返回既有结果，不重复插入。只有 `ResourceStoreCoordinator.activate(sealed_stage)` 能替换 canonical sidecar；mutable path、裸 `Path` 或尚未闭合索引的工作副本都不是可激活参数。

physical/canonical gate 只决定运行时数据权威和 exact/save 可用性；Gate C correctness 决定同 source raw CONTEXT 分类是否开放；fuzzy benchmark gate 只决定 FUZZY 外部能力是否开放；matcher gate 只决定 `TextMatcher` 哪些用途已经通过验证。fuzzy benchmark 失败时 canonical SQLite 仍继续提供 exact、已经验证的 CONTEXT 和保存，不回退 JSONL。

### Query pipeline

```mermaid
sequenceDiagram
    participant Caller
    participant Retrieval
    participant Store
    participant Candidate
    participant Scorer
    Caller->>Retrieval: TMQuery
    Retrieval->>Store: raw exact records
    Store-->>Retrieval: compatibility winner and variants
    Retrieval->>Retrieval: classify positive context
    Retrieval->>Candidate: folded source recall
    Candidate-->>Retrieval: candidate record ids
    Retrieval->>Store: batch load candidates
    Store-->>Retrieval: canonical records
    Retrieval->>Scorer: levenshtein and dice
    Scorer-->>Retrieval: similarity evidence
    Retrieval->>Retrieval: threshold dedupe stable sort limit
    Retrieval-->>Caller: QueryReport
```

单资源查询失败进入 `resource_failures`，其他资源结果继续。global limit 只在各资源结果聚合并稳定排序后应用。

## 需求追踪

以下映射覆盖 9 项需求的全部 86 条验收标准；Tasks 必须在此映射基础上生成逐条 coverage matrix，不得把范围行当作一个测试。

| 需求 | 摘要 | 组件 / 接口 |
|------|------|-------------|
| 1.1–1.8 | exact、资源状态、Excel 三态兼容 | Store raw index, LegacyTMFacade, parity tests |
| 1.9 | 迁移前后资源身份与 Active/Lookup/Update 不变 | ResourceIdentity, SnapshotBinding, Coordinator |
| 2.1–2.8 | preflight、迁移、重试、导出 | TMMigrationService, JSONLSnapshotExporter |
| 2.9–2.12 | 完整发布、切换原子可见、既有/首次激活失败恢复 | StageSealer, SealedStage, ResourceStoreCoordinator |
| 2.13 | 来源分歧不发生双向隐式覆盖 | SourceBindingMonitor, CanonicalAuthority |
| 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7 | 多译文、context、provenance | TMRecord, Store, Retrieval |
| 4.1–4.7 | 类型顺序、阈值、limit、局部失败 | TMRetrievalService, CandidateStageMetadata |
| 5.1–5.7 | 双 source、分数、显式应用安全 | TMResult, SimilarityEvidence, facade adapter contract |
| 6.1–6.10 | Match Case、Whole Word、CJK、offset | TextMatcherV1 pure algorithm, gated matcher |
| 7.1–7.7 | 本地性、隔离、恢复 | per-resource Store, QueryReport, Coordinator |
| 7.8–7.13 | divergence 期间 canonical Lookup/Update 与显式消歧 | SourceBindingMonitor, CanonicalAuthority, explicit import/rebuild |
| 7.14 | 待激活版本资源身份和来源绑定校验 | StageValidationEvidence, SealedStage, Coordinator |
| 8.1–8.7 | 100k latency/migration/RSS/report 与超限失败 | TMBenchmark, FuzzyCapabilityGate |
| 9.1–9.6 | Core 三态、基础/完整证据与用途集合 | MatcherCapabilityEvaluator, MatcherCapabilitySnapshot |
| 9.7–9.12 | fail-closed、同次快照、摘要与单一权威 | CapabilityGatedTextMatcher, validation manifest |

## 组件与接口

| 组件 | 层 | 目的 | 需求覆盖 | 关键依赖 | 契约 |
|------|----|------|----------|----------|------|
| TMContracts | Shared | 中立、版本化数据形状 | 1–9 | 无 | State, Service |
| SQLiteTMStore | Storage | per-resource canonical records | 1, 3, 7 | sqlite3 | Service, State |
| ResourceStoreCoordinator | Storage runtime | lease、drain、唯一 activation 与恢复 | 1, 2, 7 | SQLiteTMStore, SealedStage | State, Service |
| StageSealer | Storage workflow | 闭合索引、校验、fsync 并生成不可变 artifact | 2, 7 | staged Store | Service |
| SourceBindingMonitor | Storage workflow | JSONL snapshot 同源性与 divergence 状态机 | 1, 2, 7 | Store metadata | State, Service |
| TMMigrationService | Storage workflow | JSONL migration/import/rebuild/export/upgrade | 2, 7, 8 | Store, Sealer | Batch |
| CandidateRetriever | Index | recall-only candidate ids | 4, 5, 8 | Store/FTS5 | Service |
| SimilarityScorerV1 | Domain | Levenshtein/Dice/final | 4, 5, 8 | 无 | Service |
| TMRetrievalService | Domain | exact/context/fuzzy order | 1, 3–5, 7 | Store/Index/Scorer | Service |
| TextMatcherV1 | Shared domain | Unicode/CJK stable hits 纯算法 | 6, 9 | pinned data | Service |
| MatcherCapabilityEvaluator | Shared domain | 校验证据到三态快照的唯一决策 | 9 | manifest, TextMatcherV1 | State, Service |
| CapabilityGatedTextMatcher | Shared domain | 用途/选项门控和结果信封 | 6, 9 | evaluator, TextMatcherV1 | Service |
| LegacyTMFacade | Compatibility | 旧 exact/save API | 1, 5, 7 | Retrieval/Store | Service |
| TMBenchmark | Validation | performance/recall/RSS gate | 8 | all Core | Batch |

### Core contracts

```python
class TMMatchType(str, Enum):
    EXACT = "EXACT"
    CONTEXT = "CONTEXT"
    FUZZY = "FUZZY"

@dataclass(frozen=True)
class TMRecord:
    record_id: int
    source_raw: str
    target_raw: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    file_source: str | None
    provenance: tuple[tuple[str, str], ...]
    legacy_line_no: int | None
    origin_batch_id: str
    origin_ordinal: int

@dataclass(frozen=True)
class TMQuery:
    query_source: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    minimum_similarity: float
    limit: int
    resource_order: tuple[str, ...]

@dataclass(frozen=True)
class SimilarityEvidence:
    levenshtein_ratio: float
    dice_bigram: float
    final_similarity: float
    scorer_version: str = "scorer-v1"

@dataclass(frozen=True)
class ContextEvidence:
    comparable_fields: tuple[str, ...]
    matched_fields: tuple[str, ...]
    mismatched_fields: tuple[str, ...]
    strength_v1: tuple[int, int, int, int, int]

@dataclass(frozen=True)
class TMResult:
    resource_id: str
    record_id: int
    query_source: str
    matched_source: str
    target: str
    match_type: TMMatchType
    similarity: float
    similarity_evidence: SimilarityEvidence | None
    context_evidence: ContextEvidence
    provenance: tuple[tuple[str, str], ...]
    stable_tie_key: tuple[int, int]

@dataclass(frozen=True)
class QueryReport:
    results: tuple[TMResult, ...]
    resource_failures: tuple[ResourceQueryFailure, ...]
    resource_metadata: tuple[ResourceQueryMetadata, ...]

class CandidateStage(str, Enum):
    FTS_TRIGRAM = "FTS_TRIGRAM"
    GRAM_3 = "GRAM_3"
    GRAM_2 = "GRAM_2"
    GRAM_1 = "GRAM_1"
    UNION = "UNION"
    DEDUPLICATE = "DEDUPLICATE"
    TRUNCATE = "TRUNCATE"

@dataclass(frozen=True)
class CandidateStageMetadata:
    stage: CandidateStage
    input_count: int
    added_unique_count: int
    output_unique_count: int
    dropped_count: int

@dataclass(frozen=True)
class CandidateRecallMetadata:
    resource_id: str
    index_kind: str
    fuzzy_available: bool
    fuzzy_unavailable_code: str | None
    stages: tuple[CandidateStageMetadata, ...]
    union_unique_count: int
    deduplicated_count: int
    candidate_budget: int
    truncated: bool

@dataclass(frozen=True)
class ResourceQueryMetadata:
    resource_id: str
    context_available: bool
    context_unavailable_code: str | None
    recall: CandidateRecallMetadata
    scored_count: int
    returned_count: int

@dataclass(frozen=True)
class TMRecordDraft:
    source_raw: str
    target_raw: str
    speaker_raw: str | None
    context_prev_raw: str | None
    context_next_raw: str | None
    file_source: str | None
    provenance: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class TMResourceHandle:
    resource_id: str
    store: TMStore
    active: bool
    lookup: bool
    update: bool
    order: int

@dataclass(frozen=True)
class ResourceQueryFailure:
    resource_id: str
    stage: str
    error_code: str
    retryable: bool

class SourceBindingState(str, Enum):
    VERIFIED_CURRENT = "VERIFIED_CURRENT"
    VERIFIED_HISTORY = "VERIFIED_HISTORY"
    SOURCE_DIVERGED = "SOURCE_DIVERGED"

@dataclass(frozen=True)
class StoreHealth:
    healthy: bool
    schema_version: int
    generation: int
    record_count: int
    index_kind: str
    snapshot_binding_digest: str | None
    source_binding_state: SourceBindingState | None
    exact_available: bool
    context_available: bool
    fuzzy_available: bool
    diagnostic_codes: tuple[str, ...]

@dataclass(frozen=True)
class CandidateEvidence:
    record_id: int
    recall_stages: tuple[CandidateStage, ...]
    matched_grams: int
    query_grams: int
    overlap_ratio: float
    pretruncate_rank: int | None

@dataclass(frozen=True)
class CandidateRetrievalReport:
    candidates: tuple[CandidateEvidence, ...]
    metadata: CandidateRecallMetadata

@dataclass(frozen=True)
class MigrationDiagnostic:
    code: str
    stage: str
    line_number: int | None
    record_id: int | None
    safe_summary: str

@dataclass(frozen=True)
class ExportDiagnostic:
    code: str
    record_id: int | None
    safe_summary: str

@dataclass(frozen=True)
class MigrationPreflight:
    source_digest: str
    valid_count: int
    invalid_count: int
    duplicate_source_count: int
    variant_count: int
    diagnostics: tuple[MigrationDiagnostic, ...]

@dataclass(frozen=True)
class MigrationReport:
    source_digest: str
    migrated_count: int
    variant_count: int
    skipped_count: int
    diagnostics: tuple[MigrationDiagnostic, ...]
    activated_generation: int
    canonical_exact_available: bool
    context_available: bool
    fuzzy_available: bool

@dataclass(frozen=True)
class MigrationFailure:
    stage: str
    error_code: str
    retryable: bool
    diagnostics: tuple[MigrationDiagnostic, ...]
    active_generation: int | None
    original_source_unchanged: bool
    active_store_unchanged: bool
    recovery_path: Path | None

@dataclass(frozen=True)
class ExportReport:
    exported_count: int
    skipped_count: int
    destination_digest: str
    canonical_generation: int
    exported_revision: int
    snapshot_id: str
    snapshot_receipt_digest: str
    diagnostics: tuple[ExportDiagnostic, ...]

@dataclass(frozen=True)
class ExportFailure:
    stage: str
    error_code: str
    retryable: bool
    diagnostics: tuple[ExportDiagnostic, ...]
    previous_destination_unchanged: bool
    recovery_path: Path | None

type MigrationOutcome = MigrationReport | MigrationFailure
type ExportOutcome = ExportReport | ExportFailure

@dataclass(frozen=True)
class SchemaUpgradeReport:
    from_version: int
    to_version: int
    backup_path: Path
    activated_generation: int

@dataclass(frozen=True)
class SchemaUpgradeFailure:
    stage: str
    error_code: str
    retryable: bool
    active_generation: int
    active_store_unchanged: bool
    backup_path: Path | None

type SchemaUpgradeOutcome = SchemaUpgradeReport | SchemaUpgradeFailure

class SnapshotKind(str, Enum):
    MIGRATION_SOURCE = "MIGRATION_SOURCE"
    EXPLICIT_EXPORT = "EXPLICIT_EXPORT"

@dataclass(frozen=True)
class SnapshotReceipt:
    snapshot_id: str
    resource_id: str
    canonical_store_id: str
    exported_revision: int
    jsonl_digest: str
    record_count: int
    format_version: str

@dataclass(frozen=True)
class SnapshotBinding:
    configured_jsonl_path: Path
    manifest_path: Path
    snapshot_kind: SnapshotKind
    receipt: SnapshotReceipt
    binding_version: str

@dataclass(frozen=True)
class StageValidationEvidence:
    resource_id: str
    target_identity: str
    source_binding: SnapshotBinding
    snapshot_receipt_digest: str
    manifest_temp_digest: str
    schema_version: int
    fold_version: str
    index_version: str
    record_count: int
    origin_batch_count: int
    fts_count: int
    gram_counts: tuple[tuple[int, int], ...]
    exact_parity_digest: str
    integrity_ok: bool
    foreign_keys_ok: bool
    stage_file_digest: str

@dataclass(frozen=True)
class SealedArtifactRef:
    artifact_id: str
    staged_db_path: Path
    manifest_temp_path: Path
    stage_file_digest: str
    manifest_temp_digest: str

@dataclass(frozen=True)
class SealedStage:
    artifact: SealedArtifactRef
    evidence: StageValidationEvidence
    expected_prior_generation: int | None
    activation_nonce: str

@dataclass(frozen=True)
class ActivationToken:
    resource_id: str
    target_identity: str
    artifact_id: str
    sealed_stage_digest: str
    expected_prior_generation: int | None
    activation_nonce: str

@dataclass(frozen=True)
class BenchmarkContract:
    contract_version: str
    corpus_generator_version: str
    corpus_seed: int
    corpus_record_count: int
    corpus_digest: str
    exact_cohort_digest: str
    exact_min_samples: int
    fuzzy_cohort_digest: str
    fuzzy_min_samples: int
    oracle_subset_digest: str
    oracle_subset_record_count: int
    oracle_query_count: int
    top_k: int
    minimum_similarity: float
    warmup_queries_per_cohort: int
    measured_repeats: int
    percentile_method: str
    rss_scope: str
    candidate_budget_version: str
    exact_p95_gate_ms: float
    fuzzy_p95_gate_ms: float
    migration_gate_seconds: float
    peak_rss_gate_mib: float
    candidate_recall_gate: float

@dataclass(frozen=True)
class BenchmarkReport:
    contract_digest: str
    corpus_digest: str
    exact_sample_count: int
    fuzzy_sample_count: int
    percentile_method: str
    candidate_recall: float
    exact_p95_ms: float
    fuzzy_top10_p95_ms: float
    migration_seconds: float
    peak_rss_mib: float
    passed: bool
    failed_gates: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
```

所有相似度/overlap 在 `[0.0, 1.0]`，limit 为正整数，source/target/resource id 非空，handle order 唯一且非负。fuzzy 必须有 evidence；exact/context 不得伪造 scorer evidence。候选阶段计数均非负且按规定顺序出现；`UNION.output_unique_count == union_unique_count`，deduplicate/truncate 前后可对账，`scored_count <= recall.candidate_budget`，`truncated` 与 TRUNCATE dropped count 一致。fuzzy/context available 为 false 时对应 unavailable code 必须非空且不得返回该类型结果，true 时 code 必须为空。Success 必须有 digest/generation，Failure 必须有 stage/error/retryable 和资产保持标志；无法证明 unchanged 时必须给 recovery path 并 fail-stop。公开 diagnostics 只保存 code、stage、line/record id 和安全摘要，不包含正文。

### SQLiteTMStore

```python
class TMStore(Protocol):
    def exact_records(self, source_raw: str) -> tuple[TMRecord, ...]: ...
    def records_by_id(self, record_ids: tuple[int, ...]) -> tuple[TMRecord, ...]: ...
    def append(self, draft: TMRecordDraft) -> TMRecord: ...
    def export_records(self) -> Iterator[TMRecord]: ...
    def health(self) -> StoreHealth: ...
```

**连接配置**

- 每个公开 operation 先从 per-resource coordinator 获得 generation lease，再在所属线程建立短生命周期 connection；connection 不跨线程共享，也不泄露到 Store API 外。
- `journal_mode=DELETE`、`synchronous=FULL`、`foreign_keys=ON`、`busy_timeout=5000`。
- write 使用显式 transaction；失败 rollback；read cursor 尽快关闭。
- WAL capability 默认为 false；只有 SQLite fixed version、并发 recovery suite 与 writer serialization 同时满足才允许新 semantics version。

**激活协调**

- coordinator 状态为 `READY → DRAINING → ACTIVATING → READY/FAILED`，并维护 generation、active lease count 与 bounded wait；DRAINING 后不发新 lease。
- `StageBuilder` 只产出 mutable working stage。records、FTS/gram indexes、origin batches、snapshot receipt 与 manifest temporary file 全部构建后，`StageSealer` 才执行 integrity/FK/record-index count/exact parity/source binding/index version/receipt-manifest digest 校验，关闭全部连接并 fsync 文件和 parent，最后生成 `SealedStage`。
- `SealedArtifactRef` 是 module-private、由 StageSealer 工厂创建并登记到 coordinator-owned sealed registry 的 opaque 引用，绑定 staged DB path、manifest temporary path 及二者 digest；调用方不能自行构造或替换其 path。`SealedStage` 携带该引用并作为不可变、单次消费的 capability object。
- seal 后任何文件 digest 变化、registry/ref 不一致、token 重用、过期 expected generation、资源/目标路径不匹配都必须拒绝。禁止把裸 `Path`、working stage 或布尔 `validated=True` 传给 coordinator。
- `ResourceStoreCoordinator.activate(sealed_stage)` 是唯一 sidecar replace API。它只通过 registry 解析 artifact path，从 sealed evidence 生成并单次消费绑定同一 artifact id 的 `ActivationToken`，排空旧 leases，复核 resource、target identity、prior generation、DB/manifest digests 和 snapshot binding。
- coordinator 在 replace 前写同目录 durable activation journal，至少包含 activation nonce、artifact/sealed digest、expected generation、prior binding snapshot id、prior manifest digest/backup path、new receipt id、new manifest final digest，以及 `PREPARED / DB_REPLACED / MANIFEST_PUBLISHED / GENERATION_PUBLISHED` phase；成功发布后标记 consumed。重复 nonce、已 consumed token 或与 journal 不一致的 replay 均拒绝。
- 替换现有 store 前创建同目录 recovery backup；`os.replace(stage, canonical)` 后 fsync parent，重新 open 并执行 schema、digest、`integrity_check`、`foreign_key_check`、record/index count。
- reopen、receipt 与 manifest 发布/复核全部成功后才一次性发布新 generation、canonical exact/save capability 和来源绑定。crash recovery 若能复核新 DB、receipt、manifest 与同一 token，便幂等完成该 token；否则同时恢复 prior DB、prior manifest/binding，fsync parent 并重新验证，不能只恢复 DB。首次激活失败则删除/隔离未发布 manifest、保留原 JSONL 为 active legacy path，并隔离失败 sidecar。进程在 DB replace 后、generation 发布前崩溃时不得暴露半发布 generation。
- 查询/写入在 drain 超时后得到 resource-local busy failure；不得绕过 coordinator 打开 canonical path。
- physical activation 后，context correctness gate 与 fuzzy benchmark gate 分别发布；fuzzy 未通过只关闭 FUZZY，不改变 canonical exact/save 或已验证 CONTEXT。matcher capability 由另一套 evidence state machine 控制。

Activation recovery 固定为：

| Journal phase | 可复核新资产 | 恢复动作 |
|---------------|--------------|----------|
| `PREPARED` | 尚未 replace | 取消 token，prior DB/manifest/binding 不变 |
| `DB_REPLACED` | 新 DB + receipt + manifest temp 全部匹配 | 发布新 manifest，继续同一 token |
| `DB_REPLACED` | 任一新资产不匹配 | 恢复 prior DB + prior manifest/binding |
| `MANIFEST_PUBLISHED` | 新 DB/manifest/receipt 全部匹配 | 发布唯一新 generation |
| `MANIFEST_PUBLISHED` | 任一新资产不匹配 | 恢复 prior DB + prior manifest/binding |
| `GENERATION_PUBLISHED` | 新 generation 健康 | 幂等标记 token consumed |

每个恢复分支都 fsync 受影响文件及 parent directory；不得把新 manifest 留给旧 DB，也不得生成第二个 generation。

### 物理 schema

```sql
CREATE TABLE tm_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE tm_origin_batch (
    batch_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('migration', 'local_write', 'import')),
    source_digest TEXT,
    source_path TEXT,
    status TEXT NOT NULL CHECK(status IN ('staged', 'completed', 'failed')),
    valid_count INTEGER NOT NULL,
    invalid_count INTEGER NOT NULL,
    duplicate_source_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(kind, source_digest)
);

CREATE TABLE tm_snapshot_receipt (
    snapshot_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    canonical_store_id TEXT NOT NULL,
    exported_revision INTEGER NOT NULL,
    jsonl_digest TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    format_version TEXT NOT NULL,
    destination_jsonl_path TEXT NOT NULL,
    destination_manifest_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('issued', 'completed', 'cancelled')
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE tm_snapshot_binding (
    binding_id INTEGER PRIMARY KEY CHECK(binding_id = 1),
    configured_jsonl_path TEXT NOT NULL,
    manifest_path TEXT NOT NULL,
    snapshot_kind TEXT NOT NULL CHECK(
        snapshot_kind IN ('MIGRATION_SOURCE', 'EXPLICIT_EXPORT')
    ),
    snapshot_id TEXT NOT NULL,
    binding_version TEXT NOT NULL,
    FOREIGN KEY(snapshot_id) REFERENCES tm_snapshot_receipt(snapshot_id)
);

CREATE TABLE tm_record (
    record_id INTEGER PRIMARY KEY,
    source_raw TEXT NOT NULL,
    target_raw TEXT NOT NULL,
    source_fold_v1 TEXT NOT NULL,
    speaker_raw TEXT,
    context_prev_raw TEXT,
    context_next_raw TEXT,
    file_source TEXT,
    provenance_json TEXT NOT NULL,
    legacy_line_no INTEGER,
    usage_count INTEGER NOT NULL DEFAULT 0,
    last_used TEXT,
    origin_batch_id TEXT NOT NULL,
    origin_ordinal INTEGER NOT NULL,
    UNIQUE(origin_batch_id, origin_ordinal),
    FOREIGN KEY(origin_batch_id) REFERENCES tm_origin_batch(batch_id)
);

CREATE INDEX idx_tm_exact
ON tm_record(source_raw, record_id DESC);

CREATE INDEX idx_tm_context_speaker
ON tm_record(source_raw, speaker_raw, record_id DESC);

CREATE TABLE tm_gram (
    gram_size INTEGER NOT NULL,
    gram TEXT NOT NULL,
    record_id INTEGER NOT NULL,
    PRIMARY KEY(gram_size, gram, record_id),
    FOREIGN KEY(record_id) REFERENCES tm_record(record_id) ON DELETE CASCADE
);

CREATE INDEX idx_tm_gram_lookup
ON tm_gram(gram_size, gram, record_id);
```

`tm_origin_batch.kind` 是 `migration`、`local_write` 或 `import`；只有 migration/import 才需要 source digest/path，本地 append 在同一事务先建立单记录 write batch。`(kind, source_digest)` 的非空唯一约束保证同类批次幂等，`origin_ordinal` 保证批次内顺序。`tm_meta` 至少保存 schema version、resource id、canonical store id、head revision、fold/scorer/text semantics version、candidate index kind、SQLite runtime 与 activation digest；每个成功写事务推进 head revision。`tm_snapshot_receipt` 与相邻只读 manifest 保存同一规范化 ancestry receipt，证明 JSONL 快照来自 canonical 历史中的哪个 revision；ledger 额外保存本地 destination paths 以恢复任意路径 publication，这两个 path 不进入可移植 manifest 摘要。`tm_snapshot_binding` 只指向当前配置快照。issued receipt 只用于跨越 DB/JSONL/manifest 多文件崩溃窗口；completed receipt 一经发布永不修改，divergence 只作为当前 binding/file observation 派生的 `SourceBindingState`。

FTS5 fast path 使用 contentful `tm_fts(source_fold_v1, record_id UNINDEXED, tokenize='trigram case_sensitive 1')`；输入已由 fold-v1 规范化，不再叠加 SQLite tokenizer 自己的大小写语义，也不使用 external-content table。即使 FTS5 可用，`tm_gram` 仍保存 1/2-gram；无 FTS5 时再保存 3-gram。

### TMMigrationService

```python
class TMMigrationService:
    def preflight(self, source: Path) -> MigrationPreflight: ...
    def migrate(self, source: Path, destination: Path) -> MigrationOutcome: ...
    def import_snapshot(self, source: Path, resource_id: str) -> MigrationOutcome: ...
    def rebuild_from_snapshot(self, source: Path, resource_id: str) -> MigrationOutcome: ...
    def export_jsonl(self, store: TMStore, destination: Path) -> ExportOutcome: ...
    def upgrade_schema(self, store_path: Path) -> SchemaUpgradeOutcome: ...
```

- preflight 流式读取 UTF-8 JSONL，计算 SHA-256、有效/无效/重复/变体统计和可定位 diagnostics；valid row 明确定义为 JSON object 且 source/target 是非空字符串，last-valid 只在 accepted rows 中计算。
- migration 在同目录 staged path 建库；accepted rows 按原 ordinal 全部保存。
- migration/import 建立一个 origin batch；facade `save_record()`/store append 为每次本地写入建立 `local_write` batch，batch 与 record/index 必须同事务提交或回滚。
- staged DB 只有在 records 与所有声明的 candidate indexes 完成后，才执行 `foreign_key_check`、`integrity_check`、counts、exact parity probes、source binding 和 index count；通过后 close/fsync/seal，并交给 per-resource coordinator 排空连接、备份、替换、reopen 验证与发布。
- destination 是 deterministic sidecar；原 JSONL 不修改。
- 同 digest completed batch 幂等；failed batch 不作为 completed activation。
- 首次迁移激活时为原 JSONL 建立 `MIGRATION_SOURCE` receipt，把 `resource id + canonical store id + exported revision + JSONL digest + record count + format version` 同时写入 canonical ledger 与相邻 `name.jsonl.localcat-snapshot.json` manifest，再由 `SnapshotBinding` 指向这对只读资产。
- 已激活 sidecar 遇到配置 JSONL/manifest 不能与 canonical ledger 中同一 completed receipt 核对时拒绝自动覆盖并标记 `SOURCE_DIVERGED`；继续保留 last-known-good canonical，必须走显式 canonical import/rebuild。
- `resource_importer.py` 的普通 merge import 在 sidecar 已激活时必须把按输入顺序保留、未按 source 折叠的 validated rows 作为 `import` origin batch 直接提交 canonical store；不得先改 JSONL 再让 facade 猜测哪份数据更新。merge import 不修改 snapshot binding，也不清除 divergence。未激活资源继续走 legacy JSONL last-write-wins 原子导入。
- `SOURCE_DIVERGED` 期间普通 `save_record()` 继续以 `local_write` 写 canonical，同步保持 divergence 和原 JSONL 字节。只有用户明确选择“以该快照消歧”的 `import_snapshot()` 或 `rebuild_from_snapshot()` 才走全量 stage、验证、seal 和 activate；成功后更新 binding 并清除状态，任何中途失败保持 prior canonical、divergence、JSONL 与 manifest 不变。
- schema upgrade 先 `Connection.backup()` 到 timestamped recovery file，再 transaction/copy-swap。
- export 按 `record_id ASC` 写出全部 canonical variants，使最新 compatibility winner 在同 source 的历史中最后出现；字段顺序/version 固定，temporary JSONL flush/fsync 后替换，失败不覆盖有效目标。

### Canonical authority 与 JSONL snapshot binding

激活前，配置 JSONL 可以继续作为 legacy runtime store；激活后，SQLite canonical 是唯一查询与写入权威。正常 canonical append、import batch 或 head revision 变化不修改 JSONL/manifest，也不因“快照落后于当前记录”触发 divergence。`SourceBindingMonitor` 核对 JSONL digest、相邻 manifest receipt、canonical ledger receipt、resource id 与 canonical store id；当 receipt 的 `exported_revision` 是该 store 的已知历史 revision 时，状态为 `VERIFIED_HISTORY`（若等于 head 则为 `VERIFIED_CURRENT`）。它不把 JSONL 内容与当前 SQLite 全量内容做等值比较。

显式 export 分两种：

1. 导出到任意其他路径：在稳定 SQLite read snapshot 上生成 JSONL 与相邻 manifest temporary files，flush/fsync 并验证 digest/count 后发布；对应 receipt 同样按 `issued → JSONL replace → manifest replace → completed/cancelled` 写入 canonical ledger 并复用下述崩溃恢复协议，`ExportReport` 返回 canonical generation/exported revision/snapshot id。它不修改活动 binding；其失败、删除或外部修改不影响活动资源的 `SourceBindingState`，也不能清除 `SOURCE_DIVERGED`。
2. 显式导出并刷新配置 JSONL 快照：只允许当前资源未 diverged。在稳定 read snapshot 上生成并验证两个 temporary files；先在 canonical ledger 提交 `issued` receipt，再 `os.replace(JSONL)`、fsync parent，最后 `os.replace(manifest)`、fsync parent，并把 receipt/binding 标为 completed。manifest 是该双文件快照的发布标记。

恢复矩阵固定如下：

| 可观察状态 | 恢复动作 | 结果 |
|------------|----------|------|
| issued receipt，JSONL/manifest 仍是旧 completed pair | cancel issued receipt | 旧快照继续有效 |
| issued receipt，JSONL 已是新 digest，manifest 仍旧/缺失 | 由 ledger receipt 重建并发布新 manifest，再完成 binding | 新快照有效，canonical 不变 |
| issued receipt，新 JSONL 与新 manifest/receipt 一致 | 完成 binding/receipt | 新快照有效 |
| JSONL 或 manifest 与 completed/issued ledger 均不一致 | 标记 `SOURCE_DIVERGED` | canonical 继续，禁止自动覆盖 |

该协议不回滚或替换 canonical records。manifest 缺失、被外部改写，JSONL digest 被外部改写，或 receipt 的 resource/canonical identity、revision ancestry 不成立，都进入 `SOURCE_DIVERGED`；合法 local write 只推进 head revision，仍保持既有 receipt 为 `VERIFIED_HISTORY`。

显式 import/rebuild 与 export 不互相冒充：只有 import/rebuild 能在 divergence 后创建新 canonical generation、更新 source binding 并清除状态；export 永不把一个未知外部 JSONL 宣称为 canonical 来源。

### CandidateRetriever

```python
class CandidateRetriever(Protocol):
    def candidates(
        self,
        resource_id: str,
        folded_query: str,
        limit: int,
    ) -> CandidateRetrievalReport: ...
```

- query 长度 ≥3 且 FTS5 capability 可用时，把 fold-v1 query 的 unique character trigrams 分别转义为 phrase，并以 OR union 召回；按 matched unique trigram ratio、长度差、record id 稳定预排。
- FTS 结果为空、query 退化为少量重复 trigram 或 candidate pool 未达到 contract floor 时，继续 union 2-gram，再按需 union 1-gram；query 长度 1–2 直接使用对应 postings。
- 无 FTS5 时通过 1/2/3-gram postings union + overlap count 召回；不能把完整 query 的 substring MATCH 当作 fuzzy recall。
- `candidate-budget-v1 = min(8192, max(2048, result_limit * 128))`；pool 超限才按上述预排截断。
- 每次查询按执行顺序记录 FTS_TRIGRAM/GRAM_3/GRAM_2/GRAM_1/UNION/DEDUPLICATE/TRUNCATE 的 input、added unique、output unique 与 dropped counts；未执行阶段不伪造零计数。候选自身记录参与的 recall stages、matched/query grams、overlap ratio 与 pretruncate rank。
- CandidateRetriever 通过 `CandidateRetrievalReport` 同时返回候选和只属于召回阶段的 frozen `CandidateRecallMetadata`；不得提前伪造后续 scorer 或 global-limit 计数。
- TMRetrievalService 核对 recall metadata，完成评分、threshold、稳定排序和跨资源 global limit 后，才构造最终 `ResourceQueryMetadata`，补入 context capability、`scored_count` 和每资源 `returned_count`。
- query report 与 benchmark 复用同一 recall metadata contract；阶段计数、union unique、dedupe、truncate、scored、returned 必须可对账，任何负数、顺序错乱或 `scored_count > candidate_budget` 都是 validation failure。fuzzy gate 未过时 recall metadata 明确返回 unavailable code 与空阶段/候选。
- tractable oracle corpus 上，所有高于批准 threshold 的结果与真实 top-10 必须 100% 被 candidate set 覆盖；recall gate 失败不得激活 fuzzy path。
- 返回顺序不等于最终顺序；Retrieval 必须运行 scorer。

### SimilarityScorerV1

```python
class SimilarityScorer(Protocol):
    def score(self, query: str, candidate: str) -> SimilarityEvidence: ...
```

- input 使用 `fold-v1 = NFC(raw).casefold()`；exact 查询不使用该值。
- Levenshtein ratio：`1 - distance / max(len(query), len(candidate))`；双空不作为有效 TM query。
- Dice：multiset character bigram，`2 * shared / (query_grams + candidate_grams)`；相同单字符为 1，否则 0。
- `final_similarity = (levenshtein_ratio + dice_bigram) / 2`。
- 所有运算确定性；rounding 只在展示 adapter，Core 保留 float evidence。

### TMRetrievalService

```python
class TMRetrievalService:
    def query(
        self,
        resources: tuple[TMResourceHandle, ...],
        query: TMQuery,
    ) -> QueryReport: ...
```

`resources` 可包含完整 ResourceConfig adapter 集合；Retrieval 只为 `active=true && lookup=true` 的 handle 获得 store lease。`TMQuery.resource_order` 必须与 handle ids 一一对应并决定跨资源 tie order。Legacy facade 的 save path 独立只写 `active=true && update=true` handles；Lookup 不授予写权限，Update 不授予查询权限。

**分类**

1. 对每个 Active+Lookup store 执行 raw exact。
2. `record_id` 最新的有效记录是 compatibility EXACT winner，similarity=1.0。
3. context-v1 中字段仅在 query 与 record 两侧均为非空字符串时 comparable，并按 raw、区分大小写/空白的完整字符串 equality 比较。
4. `strength_v1 = (matched_count, -mismatched_count, speaker_match, prev_match, next_match)`；其他同 source variant 只有 matched_count ≥1 时成为 CONTEXT。
5. 无 context evidence 的同源非 winner 保留/导出但默认不返回。
6. 非同源 records 通过 candidate + scorer 成为 FUZZY；context-v1 strength 只影响 tie evidence。

**稳定排序**

1. type rank：EXACT、CONTEXT、FUZZY。
2. final similarity 降序；EXACT/CONTEXT 为 1.0。
3. context-v1 strength tuple 降序。
4. caller resource order。
5. record id 降序。

threshold 只过滤 FUZZY；dedupe 使用 `(resource_id, record_id)`；global limit 最后应用。

### TextMatcherV1

```python
class TextMatcherState(str, Enum):
    UNAVAILABLE = "UNAVAILABLE"
    BASIC_VALIDATED = "BASIC_VALIDATED"
    TEXT_V1_VALIDATED = "TEXT_V1_VALIDATED"

class TextMatchProfile(str, Enum):
    LEGACY_COMPAT = "LEGACY_COMPAT"
    BASIC_CONTIGUOUS = "BASIC_CONTIGUOUS"
    CONFIGURABLE_TEXT_V1 = "CONFIGURABLE_TEXT_V1"

@dataclass(frozen=True)
class SearchOptions:
    match_case: bool
    whole_word: bool

@dataclass(frozen=True)
class SearchHit:
    start_index: int
    end_index: int

@dataclass(frozen=True)
class TextMatcherCapability:
    state: TextMatcherState
    semantics_version: str | None
    supported_profiles: tuple[TextMatchProfile, ...]
    validation_summary: str | None
    unavailable_reason: str | None

@dataclass(frozen=True)
class TextMatchRequest:
    text: str
    query: str
    profile: TextMatchProfile
    options: SearchOptions

@dataclass(frozen=True)
class TextMatchSuccess:
    hits: tuple[SearchHit, ...]
    capability: TextMatcherCapability

class TextMatchRejectCode(str, Enum):
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    PROFILE_NOT_VALIDATED = "PROFILE_NOT_VALIDATED"
    OPTIONS_NOT_ALLOWED = "OPTIONS_NOT_ALLOWED"
    SEMANTICS_UNAVAILABLE = "SEMANTICS_UNAVAILABLE"

@dataclass(frozen=True)
class TextMatchRejected:
    code: TextMatchRejectCode
    safe_reason: str
    capability: TextMatcherCapability

type TextMatchOutcome = TextMatchSuccess | TextMatchRejected

class CapabilityGatedTextMatcher(Protocol):
    def capability(self) -> TextMatcherCapability: ...
    def match(self, request: TextMatchRequest) -> TextMatchOutcome: ...
```

`TextMatcherV1` 是 Core 内部纯算法和验证对象，不是对 Qt/术语/Legacy 暴露的裸 port。唯一公开执行端口是 `CapabilityGatedTextMatcher.match()`；`capability()` 只供透传/展示，真正授权仍在 `match()` 内完成，避免先查状态、后执行时状态变化的 TOCTOU。

**纯算法语义**

- `match_case=true` 直接在原文本匹配。
- false 时为 text/query 建立 casefold result；原 text 的每个 code point 产生的全部 folded code points 都映射回该原始 `[i, i+1)` span。
- folded hit 映射为覆盖其首尾 projection 的最小原始半开区间 `[start, end)`；多个 folded hit 映射到相同原 span 时去重。
- Whole Word 使用 pinned UAX #29 word-break property/evaluator；数字、下划线、combining mark、拉丁/CJK 混合由 golden vectors 固定。
- 非纯 CJK 的 Whole Word 在映射后的原文 start/end 位置执行 UAX #29 boundary 判定，不在 folded indices 上判定。
- `is_pure_cjk_v1` 要求 query 至少一个 base，所有 base 的 pinned Script 属性属于 Han、Hiragana、Katakana、Hangul；Extend、ZWJ 与 variation selector 仅可跟随这些 base。空白、标点、数字、Latin 或其他 Common/Inherited 字符使结果为 false。
- pure CJK query + Whole Word 跳过额外 boundary filter，等价连续 substring。
- overlap hit 允许；结果按原 start/end 稳定排序；空 query/零长度返回空。
- legacy preset 是 `match_case=true, whole_word=false`；basic continuous preset 是 `match_case=false, whole_word=false`。TM raw exact key 始终不调用这一 matcher。

**三态与用途矩阵**

| 状态 | LEGACY_COMPAT | BASIC_CONTIGUOUS | CONFIGURABLE_TEXT_V1 |
|------|---------------|------------------|----------------------|
| `UNAVAILABLE` | 拒绝 | 拒绝 | 拒绝 |
| `BASIC_VALIDATED` | 仅 `true/false` | 仅 `false/false` | 拒绝 |
| `TEXT_V1_VALIDATED` | 仅 `true/false` | 仅 `false/false` | 接受四种 options 组合 |

profile 是显式产品用途而非由两个 bool 猜测。空 query 在已授权 profile 下是成功的空 hits；能力或选项不受支持时 fail-closed，返回 `TextMatchRejected`，不得返回命中、静默 substring/regex/Trie fallback 或把旧兼容路径伪装为 Core success。

**证据与状态发布**

Core 内部的 `MatcherValidationEvidence` manifest 至少绑定 evidence schema、matcher artifact/build digest、semantics version、required cohort ids/digests、各 cohort pass/fail、fixture/evaluator digest、生成时间和有效期。`MatcherCapabilityEvaluator` 是唯一状态决策者：

- BASIC cohort 固定覆盖 legacy `true/false`、basic `false/false` Unicode case-fold 连续搜索、case-fold expansion 后的原文 offsets、稳定排序、空查询和对应 golden vectors。
- full cohort 在同一 semantics version 上覆盖 Match Case/Whole Word 四组合、Unicode case-fold/word boundary、数字、下划线、标点、mixed script 和 pure CJK golden vectors。
- BASIC 任一证据缺失、失败、过期，或与实现 artifact/semantics 不一致 → `UNAVAILABLE`。
- BASIC 有效但 full cohort 无效 → `BASIC_VALIDATED`，profiles 恰为 legacy + basic。
- BASIC 与 full cohort 在同一 semantics version 有效 → `TEXT_V1_VALIDATED`，profiles 恰为三者全集。
- evidence、fixture、evaluator、artifact 或 semantics version 变化会重新求值并允许升级/降级；不存在 caller `set_state()`、`validated=True` 或消费方 override。

可用状态的 `semantics_version` 与 `validation_summary` 必须非空；UNAVAILABLE 的 profiles 为空且 `unavailable_reason` 非空。公开 summary 只是规范化内部 evidence 的不透明安全 digest/token，不暴露正文或供消费方重新判定的 `basic_passed/text_passed` 字段。

每次 `match()` 开始只原子读取一次 immutable capability snapshot，用它校验 profile/options、选择对应 semantics 并贯穿执行；无论成功或拒绝，都把同一 snapshot 放入 outcome。运行中 publisher 可原子替换全局 snapshot，但在途调用完成于旧 snapshot，后续调用看到新 snapshot。

项目字段遍历、章节范围、speaker display 和导航属于 Controller；术语长词优先、非重叠仲裁和资源顺序属于 Glossary adapter。它们只把中立 profile/options 交给 Core，不得定义本地 readiness、解析 validation summary、读取 test fixture 或用 `hasattr()` 推断能力。

### LegacyTMFacade

- ResourceConfig 继续暴露旧 JSONL compatibility path 与既有资源身份；facade 通过内部 deterministic mapping 找到 `name.jsonl.sqlite3`，不得要求 UI 改写资源路径。
- sidecar 未完成首次 physical activation 时继续旧 engine 行为；一旦激活，exact/query/save 全部只获得 canonical generation lease，JSONL 永不再作为隐式 fallback。
- `SourceBindingMonitor` 核对配置 JSONL digest、相邻 manifest 与 canonical ledger receipt；不是同一 canonical 的 completed/可恢复 issued receipt 时保留 last-known-good canonical、报告 `SOURCE_DIVERGED`，不静默切换数据源或双向覆盖。
- `query_exact(source)` 只返回 compatibility EXACT winner，不暴露 context/fuzzy。
- `save_record()` 对 canonical store append，最新 record 成为之后的 exact winner。
- sidecar open/health 失败由 coordinator 恢复 prior generation 并报告；只有首次 physical activation 尚未成功时才可继续 legacy JSONL。
- fuzzy gate 失败只使 FUZZY unavailable；已通过 Gate C 的 CONTEXT、canonical exact/save 和 Excel 三态继续运行。matcher gate 与 facade exact/save 独立。
- Excel `LogicController` 不调用 full query，因此仍只有三态。

### TMBenchmark

```python
class TMBenchmark:
    def run(self, contract: BenchmarkContract) -> BenchmarkReport: ...
```

- machine-readable `benchmark_tm_contract.json` 必须与 `BenchmarkContract` 一致；`benchmark-v1` 固定 generator/seed/digests、100,000 records、exact ≥1,000 queries、fuzzy ≥200 queries。
- deterministic corpus 包括 multilingual/CJK/short/duplicate/multi-target/context/near-edit/miss cohorts；query cohort 由 digest 固定，不允许运行时挑选有利样本。
- oracle subset 固定 5,000 records/200 queries，minimum similarity=0.60、top-k=10；above-threshold 全集与真实 top-10 candidate recall 均须 100%。
- 每 cohort 先执行 100 个不计时 warmup，measured repeats=1；p95 使用 nearest-rank `ceil(0.95*n)`，不得用插值或先聚合 batch average。
- migration 计时包含 parse、insert、index build、validation、fsync 与 activation/reopen health。
- warm exact 与 fuzzy top-10 分开测 `perf_counter_ns`，报告 p50/p95/max/sample count。
- migration/query 在独立 child process 运行；RSS scope 从子进程启动到完成，包含 DB open/parse/index/query、排除预生成 fixture，报告各 run 峰值中的最大值。
- 报告 Python、SQLite、UCD、FTS5、CPU、RAM、OS、corpus digest、warmup、percentile definition、scorer/index config。
- 硬门：candidate oracle recall=100%；exact p95 ≤50 ms；fuzzy p95 ≤500 ms；migration ≤120 s；RSS ≤512 MiB。
- 样本数、digest、环境或 contract 字段不一致直接失败，不能只比较四个性能数字。
- FTS5 trigram fast path 与无 FTS5 的 1/2/3-gram fallback 必须分别执行和报告。fallback 在 100k 上超限时按 Requirement 8.7 把对应能力标记失败，不在 Design 阶段猜测、放宽门限或用 fast path 的成功掩盖失败。

## 数据一致性与迁移

### 一致性边界

- 一个 SQLite file 是一个 TM resource 的 transaction/故障边界。
- 多资源写入不伪造跨文件 transaction；调用者逐资源接收结果。
- record append 与 candidate index update 在同一 DB transaction。
- FTS5 contentful rows 和 record insert 同 transaction；health check 比较 counts。
- physical activation 后 sidecar 是唯一 canonical runtime；exact/read/write、import 和 index maintenance 都经 generation lease 进入 SQLite。
- canonical store id 标识一条逻辑血缘；普通写入和保持语义的 schema upgrade 保留该 id 并单调推进 head revision，显式从外部快照 import/rebuild 则创建新 store id 和新 binding。
- 原 JSONL 与相邻 receipt manifest 是绑定到某个 canonical revision 的 immutable recovery/import/audit evidence，不是同步 mirror；canonical 正常写入既不修改快照也不构成 divergence。
- 配置 JSONL/manifest 外部变化、删除，或与 canonical ledger identity/digest/ancestry 不一致才进入 `SOURCE_DIVERGED`；状态期间 canonical Lookup/Update 继续，只有显式 import/rebuild 成功才能清除。
- physical、fuzzy、matcher 三个 gate 分别进入 StoreHealth/QueryReport/Matcher outcome，禁止用单一 `ready` 混合表达。

### Schema version

- schema version 单调增加，拒绝打开高于当前支持的版本。
- fold/scorer/text/index version 独立保存；任一变化可触发 index rebuild，不改 raw records。
- upgrade 失败恢复 backup 或保持旧 DB 未激活。

## 错误处理

| 类别 | 示例 | 响应 |
|------|------|------|
| Input | 空 source/target、无效 threshold/limit | typed validation error |
| Migration | malformed JSONL、磁盘满、parity 失败 | `MigrationFailure` 保留 JSONL/active generation，报告 stage/retryable/diagnostics |
| Store | locked、corrupt、schema too new | resource-local failure，其他资源继续 |
| Activation | stale/used/mutated token、资源或 binding 不符 | 拒绝替换，保留 prior generation，隔离 stage |
| Snapshot | 配置 JSONL digest/binding 不符 | `SOURCE_DIVERGED`，canonical Lookup/Update 继续，禁止隐式覆盖 |
| Capability | FTS5 不可用 | gram fallback + candidate capability metadata |
| Context gate | raw context classification correctness 未过 | canonical exact/save 继续，CONTEXT 不对外开放 |
| Fuzzy gate | recall/benchmark 未过 | canonical exact/save 与已验证 CONTEXT 继续，仅 FUZZY 不对外开放 |
| Matcher gate | unavailable/profile/options 未验证 | fail-closed outcome + 同一 capability snapshot |
| Index | count mismatch、query error | 不使用该 index；health/report 明示 |
| Query | 单资源失败 | partial QueryReport + failure |
| Export | fsync/replace 失败 | `ExportFailure` 标明旧目标是否保持及 recovery path |
| Unicode | unsupported semantics version | 拒绝，不回退未版本化行为 |

公开错误不包含完整 source/target；默认日志记录 resource id/path、stage、counts、SQLite code 和 exception category。

## 安全考虑

- 所有数据和 benchmark corpus 保持本地，不调用网络、账号或 telemetry。
- SQL 仅使用 parameter binding；表/pragma 名来自封闭常量，不拼接用户输入。
- provenance JSON 在边界验证类型/长度；不执行其内容。
- migration/export path 由调用者明确提供并规范化，不跟随未知 sidecar marker。
- 原 JSONL、backup 和 DB 文件权限继承本地应用资源策略。
- SQLite extension loading 保持关闭；FTS5 只使用编译内置 capability。

## 性能与可扩展性

- exact B-tree raw index 是独立 fast path，不扫描 candidate index。
- candidate batch fetch 避免每 record 一次 SQL。
- migration 先 bulk insert、后建大索引；transaction 与 RSS 由 100k gate 约束。
- FTS5/gram 只控制 recall set；默认 candidate count 写入 versioned contract。
- read connection 不长时间持有 transaction；rollback journal 下 write transaction 保持短小。
- WAL 不作为首版性能优化；升级前必须先解决当前 3.51.2 advisory。

## 测试策略

### 单元测试

- contracts：全部 invariants、tuple、Enums、range、双 source。
- store：schema、raw exact order、append、context fields、foreign keys、rollback。
- scorer：golden distance/bigram、empty/one-char/Unicode、final average、determinism。
- TextMatcher：casefold expansion、original offsets、Whole Word、CJK、数字、下划线、combining marks。
- matcher capability：三态 evidence matrix、过期/build/fixture/version mismatch 降级、profile×state×options、single-snapshot race、opaque/no-content summary。
- candidate：FTS5 ≥3、short gram、no-FTS fallback、recall evidence、阶段计数/union/dedupe/truncate 对账。

### 集成测试

- JSONL preflight/migrate/retry/export，原字节不变、`export → migrate` record/variant/exact-winner parity。
- mutable stage 建完全部索引后才能 seal；seal 后篡改、token 重用、stale generation、错资源/路径均拒绝。
- activation 在 DB replace/manifest publish/generation publish 各 phase 崩溃时，要么幂等完成同一 token，要么同时恢复 prior DB 与 prior manifest/binding。
- physical activation 成功而 fuzzy benchmark 失败时，SQLite exact/save 继续且 JSONL 不再成为 runtime。
- QueryReport 分别区分“CONTEXT/FUZZY 可用但本次无命中”与“对应 gate 未开放”，并在 global limit 后对账每资源 returned count。
- canonical 正常写入不触发/清除 divergence；外部 JSONL 变化触发，显式 import/rebuild 失败保持三方资产，成功才换 generation 并清除。
- 配置快照 refresh 的 issued receipt 在 crash 后按 digest 完成、取消或进入 divergence，不回滚 canonical。
- same-source variants：winner EXACT、positive context CONTEXT、no evidence retained-only。
- multi-resource query：stable global order、partial failure、resource provenance。
- facade 激活前后 `query_exact/save_record` 一致。
- corrupt/locked/schema upgrade/backup recovery 和 last-known-good。

### Compatibility regression

- `LogicController` 三态、TM priority、Excel formatter 不出现第四状态。
- Active+Lookup 与 Active+Update 资源集合保持。
- Qt 现有 exact suggestion journey 只在后续 adapter 任务重验，不由 Core 修改 Qt。
- 架构守卫禁止 Qt/术语/Legacy 定义 matcher readiness、解析 validation summary 或绕过 gated matcher。
- `tm_engine.py`、`logic_controller.py`、`stress_runner.py`、`translation_runner.py` 旧自检。

### Performance

- 100k deterministic contract 全部四项 hard gate。
- candidate recall 与 brute-force oracle；任何 above-threshold/top-10 miss 都是 hard failure。
- FTS5 与 fallback index 分别报告，不用一条成功结果掩盖另一 capability。
- rollback journal 下 exact/fuzzy/migration；未来 WAL 另立对比和 recovery suite。

## 实施与激活顺序

1. **Gate A — Contracts / algorithms**：冻结 1–9 公共契约、TextMatcher pure algorithm、capability evidence evaluator 和 scorers。
2. **Gate B — Canonical physical store**：完成 SQLite schema、snapshot binding、mutable stage、完整 candidate index、StageSealer、coordinator 和 exact parity；只接受 SealedStage 原子激活。
3. **Physical activation**：成功后立即把 exact/query/save compatibility facade 切到 canonical SQLite，并完成 Excel/core regression；只有首次激活失败且无 prior canonical 时才继续原 JSONL。
4. **Gate C — Retrieval correctness**：先以 raw same-source vectors 验证并开放 CONTEXT，再完成 phased candidate metadata、FUZZY pipeline、事务和局部失败矩阵；oracle recall 未过只关闭 FUZZY。
5. **Gate D — benchmark-v1**：FTS5 与 fallback 分别达到 100k hard gates 后，才发布相应 fuzzy capability；超限显式失败但不撤销 canonical authority。
6. **Matcher gate**：由独立 validation manifest 发布 UNAVAILABLE/BASIC/TEXT_V1；不从 sidecar、FTS5 或 benchmark 状态推断。
7. 后续独立 Qt integration commit 只消费 Core capability-gated matcher 和 full query，不创建第二权威。

任何 physical gate 失败都不得发布部分 sidecar；任何 fuzzy/matcher gate 失败都不得伪装能力或把已激活 canonical 回退为 JSONL。不得以已有 similarity 字段、可打开数据库、测试文件存在或 FTS5 可用冒充 Feature 5 完成。
