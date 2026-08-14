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
    RetrievalEvidence[Retrieval validation evidence] --> RetrievalGate[Retrieval capability publisher]
    RetrievalGate --> Retrieval
    Migration --> Coordinator
    Retrieval --> Facade[Legacy TM facade]
    Coordinator --> Facade
    Benchmark[TM benchmark] --> RetrievalGate
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
├── tm_schema_upgrade.py             # schema upgrade copy 数据面与 pending/reported artifact 协议
├── tm_snapshot_artifacts.py         # snapshot artifact namespace/proof/handoff primitives
├── tm_candidate_index.py            # FTS5/gram candidate retrievers
├── tm_similarity.py                 # Levenshtein、Dice、scorer-v1
├── tm_retrieval.py                  # exact/context/fuzzy pipeline 与聚合
├── tm_retrieval_capability.py       # Gate C/D evidence evaluator、原子能力快照与发布
├── tm_retrieval_validation.py       # Gate C 固定向量执行、结果重算与 manifest 生成
├── tm_migration.py                  # JSONL preflight/migrate/export/upgrade
├── text_matcher.py                  # text-v1 纯算法、fold projection 与 hit logic
├── matcher_capability.py             # evidence evaluator、三态发布与 gated port
├── unicode_word_break_data.py       # generated pinned property tables
├── tm_engine.py                     # 激活 gate 后的 compatibility facade
├── resource_importer.py             # 已激活资源调用 canonical import port
├── tm_benchmark.py                  # benchmark-v1 确定性语料、cohort 与冻结输入契约
├── tm_benchmark_latency.py          # exact/fuzzy 逐查询延迟样本与 nearest-rank 统计
├── tm_benchmark_process.py          # 独立子进程迁移、reopen 与全生命周期 RSS 采样
├── tm_benchmark_oracle.py           # 固定 subset 全扫描 oracle 与 candidate recall 对账
├── tm_benchmark_query_process.py    # 按迁移 artifact identity 重开真实 store 的查询子进程
├── tm_benchmark_gate.py             # TMBenchmark 组合入口、双路径报告与 Gate D 发布
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
    ├── test_tm_retrieval_capability.py
    ├── test_tm_retrieval_validation.py
    ├── test_tm_migration.py
    ├── test_text_matcher.py
    ├── test_matcher_capability.py
    ├── test_tm_engine_compat.py
    └── test_tm_benchmark_contract.py
```

Activation 模块在 Task 5.9 闭合完整恢复矩阵后、Cluster D 统一复审前做行为保持型提取。`tm_sqlite_store.py` 在 Feature 5 内继续保持既有 `ResourceStoreCoordinator` 导入入口，但不再拥有 journal/terminal canonical codec、exclusive temporary/replace/fsync 原语或逐 phase 恢复/回滚实现；新模块不得反向导入 `SQLiteTMStore`，只能消费 frozen contracts 与显式窄端口。提取不得修改 journal phase、错误码、token/nonce 单次语义、fault-injection 顺序或 public lease/activation 行为；原 Cluster D characterization/failure matrix 必须在移动前后使用同一断言通过。`tm_contracts.py` 与 `tm_stage_sealer.py` 不属于本次提取范围，待 Feature 5 契约面稳定后另行评估。

Cluster E 行为闭合后、Cluster F 开始前增加 schema-upgrade 行为保持型边界提取。`tm_schema_upgrade.py` 仅拥有 v1→v2 copy 数据面、backup/locator 的 pending→reported 持久化协议、strict locator file proof 与纯候选事实校验；它不反向导入 `tm_sqlite_store.py` 或 `tm_migration.py`，只通过显式值、callback/窄端口消费 schema DDL 和 canonical ancestry 证明。`ResourceStoreCoordinator` 仍在 `tm_sqlite_store.py` 独占 ticket/locator snapshot 所有权、lease/drain/state transition、activation guard 与 cold-recovery root 选择；`TMMigrationService.upgrade_schema()` 的公开入口、成败编排和 report/failure 构造仍在 `tm_migration.py`。原模块对已有 private 导入与 fault-injection patch seam 保留 late-bound compatibility wrapper，不得藉移动改变分支顺序、异常码、cleanup 顺序或磁盘效果。`tm_contracts.py`、`tm_stage_sealer.py`、canonical ancestry 单一证明实现与通用 coordinator 状态机不纳入此次提取；`try/except/if/raise` 简化属于正交的后续治理，不与等价移动同一提交。提取前后必须用同一 Cluster E failure/interleaving matrix、公开 API 契约、import-boundary 守卫和 fresh 全量回归证明等价。

Cluster F 行为、命名空间与冷恢复矩阵闭合后增加 snapshot artifact 行为保持型边界提取。`tm_snapshot_artifacts.py` 只拥有 deterministic JSONL/manifest/temp/recovery family、root→parent no-follow directory descriptor 绑定、strict regular/single-link identity+digest proof、exclusive temporary/recovery copy、replace/cleanup 原语与 durable handoff 值编解码；它不反向导入 `tm_sqlite_store.py`、`tm_migration.py` 或 `tm_snapshot_recovery.py`。`TMMigrationService` 仍独占公开 export/refresh 编排、canonical snapshot 使用和 report/failure 构造；`tm_snapshot_recovery.py` 仍独占 receipt 分类、reconciliation、terminal replay 和 divergence 决策；`tm_sqlite_store.py` 仍独占 ledger/binding SQL、transaction、generation 与 coordinator 状态。已有 owner 导入和 fault-injection seam 通过 late-bound compatibility wrapper 保留，不得改变错误码、调用/清理顺序、durable handoff 生命周期、交易边界或磁盘效果。该门不设行数指标；只用 owner 责任减少、无反向导入、Cluster F 同一断言与 fresh 全量回归判定成功。异常分支简化、错误分类重新设计、`tm_contracts.py`/`tm_stage_sealer.py` 拆分与公开 API 调整全部排除。

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
| SnapshotArtifactProtocol | Storage mechanism | snapshot artifact namespace、identity proof、replace/cleanup 与 handoff codec | 2, 7 | frozen contracts, stdlib filesystem | Service |
| CandidateRetriever | Index | recall-only candidate ids | 4, 5, 8 | Store/FTS5 | Service |
| SimilarityScorerV1 | Domain | Levenshtein/Dice/final | 4, 5, 8 | 无 | Service |
| TMRetrievalService | Domain | exact/context/fuzzy order | 1, 3–5, 7 | Store/Index/Scorer | Service |
| RetrievalCapabilityEvaluator | Domain validation | Gate C correctness 与逐执行路径 Gate D evidence 的唯一判定 | 4, 5, 7, 8 | frozen contracts, validation evidence | State, Service |
| RetrievalCapabilityPublisher | Domain runtime | 原子发布 CONTEXT 与逐路径 FUZZY 不可变快照 | 4, 7, 8 | evaluator | State, Service |
| RetrievalValidation | Domain validation | 从固定输入重新执行 Gate C vectors 并生成 identity-closed manifest | 3–5, 7 | Retrieval pure functions, frozen contracts | Batch |
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
    previous_destination_preservation: AssetPreservationEvidence
    recovery_locators: tuple[RecoveryLocator, ...]
    publication_committed: bool = False
    publication_commit_ambiguous: bool = False

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

所有相似度/overlap 在 `[0.0, 1.0]`，limit 为正整数，source/target/resource id 非空，handle order 唯一且非负。fuzzy 必须有 evidence；exact/context 不得伪造 scorer evidence。候选阶段计数均非负且按规定顺序出现；`UNION.output_unique_count == union_unique_count`，deduplicate/truncate 前后可对账，`scored_count <= recall.candidate_budget`，`truncated` 与 TRUNCATE dropped count 一致。fuzzy/context available 为 false 时对应 unavailable code 必须非空且不得返回该类型结果，true 时 code 必须为空。Success 必须有 digest/generation，Failure 必须有 stage/error/retryable 和资产保持标志；普通失败无法证明 unchanged 时必须给 recovery locator 并 fail-stop。若 receipt 与新 pair 已 durable commit、禁止回滚旧 destination，但 deterministic cleanup/handoff 尚未闭合，`ExportFailure.publication_committed` 为 true：结果仍 fail-stop，保留已知 before/observed digest，且不得给出会暗示恢复旧 destination 的 locator；若 completion probe 本身失败、ledger commit 状态不可判定且自动回滚同样不安全，则互斥地设置 `publication_commit_ambiguous`，以同样的 fail-stop/no-locator 规则保留真实证据但绝不宣称已经 commit。原 destination 不存在时才可使用 `NOT_APPLICABLE`。公开 diagnostics 只保存 code、stage、line/record id 和安全摘要，不包含正文。

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
- `TMRetrievalService` 的一次单资源查询是一个组合 operation：只取得一次 query lease，并在其生命周期内通过 module-private、只读的 generation view 完成 health、raw exact、candidate recall 与 candidate record 批量读取。view 不暴露 append/export/activation/update 端口，不得在内部重新取得 lease，退出后立即失效；进入 `DRAINING` 前已经签发的 view 可完成当前查询并阻塞 generation 发布，`DRAINING` 后不得签发新 view。该约束保证同一资源的查询事实来自一个完整 generation，但不要求跨多个短连接持有同一个 SQLite transaction。
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

### 激活血缘标记（activated-lineage marker）

物理激活成功后的资源/目标必须留下一个最小的、确定性的、邻接的、只写一次的激活血缘标记，作为“该资源/目标已经成功跨越物理激活”的持久事实。它**不是**第二套可变的 canonical 权威，不保存任何用户正文，也不随 generation 变化；后续 generation、显式 import/rebuild、schema upgrade 都保留同一个标记事实。

**身份与路径**

- 标记路径确定且不可由调用方指定：`identity.canonical_sidecar_path` 同目录下的 `.{sidecar 名}.localcat-activated-lineage.json`，临时文件为 `<标记路径>.tmp`（同目录、确定性命名）。
- 标记**只**绑定稳定血缘事实：`lineage_version`、`resource_id`、`target_identity` 与 `record_digest`。它**不**绑定 `canonical_store_id` 或任何可变 coordinator 身份：显式 import/rebuild 可以创建新的 canonical store id，后续 generation 也不改变这一只写一次的标记事实。

**严格 codec（v1）**

- `lineage_version="activated-lineage-v1"`、`resource_id`、`target_identity`、`record_digest` 四个字段；`record_digest` 是其余三个字段经 `_stable_digest` 计算的 SHA-256。
- 文件必须是 canonical JSON（`sort_keys`、无重复键、禁止非有限数字、无多余字段），只读/重放时逐字节复核序列化与摘要。
- 标记文件必须是 regular 单链接文件；symlink、hardlink、目录或其他外来条目一律 fail-closed，绝不跟随、使用或覆盖。读取用 `O_NOFOLLOW` + open-time fstat + post-read lstat 复核同一 inode；发布握手期允许 final/temp 同 inode 的两链接中间态，仅由配对 inode 证明接受。

**发布与重放顺序**

- 首次物理激活的发布顺序固定为：完整 active set 复核通过且视图 withheld 在 `ACTIVATING` → `GENERATION_PUBLISHED` journal 落盘 durable → 同一 active set 再次复核 → token consumed → **最后**确保（写入或严格重校验）血缘标记 → `READY`。标记失败时 view 继续 withheld 在 `ACTIVATING`，完成的 journal 是冷恢复权威，fresh recovery 幂等补写标记后收尾；rollback 与取消绝不清除标记。
- 冷恢复发布（`MANIFEST_PUBLISHED` 推进 generation）同样先重新证明 active set、落盘 durable `GENERATION_PUBLISHED`、再次复核，之后才确保标记；失败时收回 view 停在 `ACTIVATING`。终态重放（`GENERATION_PUBLISHED`）先重放 view 并复核 active set，再确保标记，最后清理 journal-owned backups。已存在且合法的标记绝不重写。
- 标记发布是原子 no-clobber 协议：exclusive 确定性临时文件（`O_EXCL` + 全量写入 + fsync）→ `os.link` 发布 final（final 已存在时 `FileExistsError` 即外来 final，fail-stop 且绝不覆盖，仅身份绑定清理自有临时）→ parent fsync → 仅当 final/temp 仍是同一 inode 且 `st_nlink==2` 时 unlink 临时 → 再次 parent fsync → 最终单链接逐字节重校验。崩溃重放只接受配对握手态（final/temp 同 inode 且字节等于确定性 payload）并完成 unlink；其他 symlink/hardlink/外来 final 或 temp 一律 fail-closed；字节精确的 owned 单链接临时文件恢复发布流程。
- `DB_REPLACED → MANIFEST_PUBLISHED → GENERATION_PUBLISHED` 的恢复/终态重放路径在报告 COMPLETED 前幂等补写或严格重校验标记。
- rollback 与 PREPARED 取消**绝不**清除标记。PREPARED 取消的 lineage 一致性：取消回退到 prior canonical generation 仅当 durable 标记存在并通过稳定身份重校验；第一次激活（无 prior）取消时标记 final 与 temp 必须**都不存在**——任何外来、篡改、hardlink 或残留标记/temp 一律 fail-closed，防止把从未激活的 legacy 资源变成声称已激活。
- 无 journal、无 terminal 的冷发现：无论进程内是否已有 live view，都先对磁盘重新证明 canonical pair 与标记。无标记且无 canonical pair 是真正的从未激活 legacy（返回 `None`/READY，此时 live view 非空同样 fail-stop）；有标记但 pair 缺失/部分/篡改时 fail-stop 并报告恢复失败，绝不静默返回 `None`；有 pair 但无有效标记（无 transition record 的权威）绝不静默信任，同样 fail-stop。

**取消候选隔离（quarantine closure）**

- PREPARED 取消的候选 DB/manifest 退役进确定性隔离目录：路径缺失只在该 journal 记录的 inode 已作为 regular 单链接条目存在于该确定性隔离目录（候选或 canonical basename，扫描限定单目录）时才被接受；inode 缺失（外部删除/移动）一律 `ACTIVATION.QUARANTINE_MISSING` 非重试 fail-stop，不再有 authority-path 兜底。隔离条目必须 regular 单链接，任何外来条目 `ACTIVATION.QUARANTINE_FOREIGN` fail-stop；隔离目标绝不被覆盖。

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
    source_fold_length INTEGER NOT NULL CHECK(source_fold_length >= 0),
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
    term_frequency INTEGER NOT NULL CHECK(term_frequency > 0),
    PRIMARY KEY(gram_size, gram, record_id),
    FOREIGN KEY(record_id) REFERENCES tm_record(record_id) ON DELETE CASCADE
);

CREATE INDEX idx_tm_gram_lookup
ON tm_gram(gram_size, gram, record_id);

CREATE TABLE tm_candidate_block (
    block_id INTEGER PRIMARY KEY,
    first_record_id INTEGER NOT NULL,
    last_record_id INTEGER NOT NULL,
    record_count INTEGER NOT NULL CHECK(record_count > 0),
    min_source_fold_length INTEGER NOT NULL,
    max_source_fold_length INTEGER NOT NULL
);

CREATE TABLE tm_gram_block_max (
    gram_size INTEGER NOT NULL CHECK(gram_size IN (1, 2)),
    gram TEXT NOT NULL,
    block_id INTEGER NOT NULL,
    max_term_frequency INTEGER NOT NULL CHECK(max_term_frequency > 0),
    PRIMARY KEY(gram_size, gram, block_id),
    FOREIGN KEY(block_id) REFERENCES tm_candidate_block(block_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_tm_gram_block_lookup
ON tm_gram_block_max(gram_size, gram, block_id);
```

`tm_origin_batch.kind` 是 `migration`、`local_write` 或 `import`；只有 migration/import 才需要 source digest/path，本地 append 在同一事务先建立单记录 write batch。`(kind, source_digest)` 的非空唯一约束保证同类批次幂等，`origin_ordinal` 保证批次内顺序。`tm_meta` 至少保存 schema version、resource id、canonical store id、head revision、fold/scorer/text semantics version、candidate index kind、SQLite runtime 与 activation digest；每个成功写事务推进 head revision。`tm_snapshot_receipt` 与相邻只读 manifest 保存同一规范化 ancestry receipt，证明 JSONL 快照来自 canonical 历史中的哪个 revision；ledger 额外保存本地 destination paths 以恢复任意路径 publication，这两个 path 不进入可移植 manifest 摘要。`tm_snapshot_binding` 只指向当前配置快照。issued receipt 只用于跨越 DB/JSONL/manifest 多文件崩溃窗口；completed receipt 一经发布永不修改，divergence 只作为当前 binding/file observation 派生的 `SourceBindingState`。

FTS5 fast path 使用 contentful `tm_fts(source_fold_v1, record_id UNINDEXED, tokenize='trigram case_sensitive 1')`；输入已由 fold-v1 规范化，不再叠加 SQLite tokenizer 自己的大小写语义，也不使用 external-content table。即使 FTS5 可用，`tm_gram` 仍保存带 multiset term frequency 的 1/2-gram；无 FTS5 时再保存 3-gram。`candidate-proof-block-v1` 以每 256 个连续 record-id slot 形成固定 block；`tm_candidate_block` 与 `tm_gram_block_max` 必须由同一 record/index 事务精确维护并可从 canonical rows 完整重算。summary 可以因跨 record maxima 而宽松，但不得低于块内任一真实 term frequency。

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

issued receipt 的 versioned artifact handoff 同时记录排他 temp/recovery copy 身份、其真实直接父目录的 device/inode，以及发布前 final pair 的 identity/digest/明确缺席事实。handoff 必须跨越 completed/cancelled 终态事务继续存在；只有先验证 terminal receipt 的 resource/canonical identity、revision ancestry、配置/任意路径分类、authority alias、完整真实父目录链与 durable parent identity，再按 exact identity+digest 清理 owned artifact、通过绑定该 parent identity 的 no-follow directory descriptor 执行 fsync、严格复核父目录未替换且四个 deterministic artifact path 均缺席后，才可清除 handoff。任何外来同字节 inode、symlink、hardlink、目录、父目录替换或 fsync 失败都保持文件与 handoff、返回 `BLOCKED`/cleanup-pending failure；不得在 cleanup 未闭合时报告成功。预先锁存的配置 divergence 只阻止 configured terminal replay，不得永久阻塞与 binding/divergence 无关的任意路径 terminal cleanup。任意路径 export replay 始终不改变 active binding 或 divergence；配置 refresh replay 仍受资源级可重入 observation gate 串行化。

路径字符串和相同字节不是命名空间授权。每个 replace/rename/delete 必须遵循同一 mutation-proof 模型：从稳定 root 逐段 no-follow 绑定直接父目录，以 durable handoff/receipt 确定允许的 source、destination 和先前缺席/身份事实，在最后一个可观测 fault seam 返回后、紧邻 mutation 之前同时复证 source 与 destination，使用该 dirfd 执行变更，然后复核 final 就是已交接 source inode、fsync parent 并持久化状态转换。父目录 rename/ABA、source 或 destination 在复证窗口被替换、多链接、同字节外来 inode 或无法复核的 post-mutation 结果均不得继续完成/清理，必须保留 durable replay 证据并 fail-closed。

显式 import/rebuild 与 export 不互相冒充：只有 import/rebuild 能在 divergence 后创建新 canonical generation、更新 source binding 并清除状态；export 永不把一个未知外部 JSONL 宣称为 canonical 来源。

### CandidateRetriever

```python
class CandidateRetriever:
    def candidates(
        self,
        resource_id: str,
        store: SQLiteTMStore,
        folded_query: str,
        *,
        result_limit: int,
    ) -> CandidateRetrievalReport: ...

    def candidates_from_view(
        self,
        resource_id: str,
        view: SQLiteTMQueryView,
        folded_query: str,
        *,
        result_limit: int,
    ) -> CandidateRetrievalReport: ...
```

- query 长度 ≥3 且 FTS5 capability 可用时，把 fold-v1 query 的 unique character trigrams 分别转义为 phrase，并以 OR union 形成 fast seed；无 FTS5 时按 GRAM_3/2/1 形成 fallback seed。seed 只决定实际 execution path、初始上界队列与可诊断阶段，不能作为 scorer-v1 完备性的证明。
- canonical index 在 record/index 同一事务保存 `source_fold_length`、字符与 bigram 的 multiset term frequency，以及固定 record block 的长度范围和各 term 最大频次。block summary 只提供保守上界；缺行、重复、计数不守恒或 summary 低估都使该资源 fail-closed。
- 对 fold-v1 query 与一个 record，令长度为 `m/n`、字符 multiset 交集为 `C`、bigram multiset 交集为 `I`、bigram 总数为 `Bq/Br`。编辑距离安全下界为 `max(abs(m-n), max(m,n)-C, ceil((Bq+Br-2I)/4))`；由此得到 Levenshtein ratio 上界，再与精确 bigram Dice 平均得到 scorer-v1 上界。单字符 Dice 沿用 scorer-v1 特例。实现必须以穷举/随机对照证明上界从不低估真实分数。
- `proof-query-v2` 保留 256-slot block 作为完整性与稀疏遍历单元；CandidateRetriever 在同一 generation view 内优先按 `(score_upper_bound DESC, record_id DESC)` best-first 打开 block。若保守 block maxima 使大量 block 仍可越过阈值，则切换到 `proof-traversal-v2` 两阶段精化，而不是逐 block 建连或一次性计算 100k 条完整字符/bigram exact frontier。
- 两阶段 phase 1 在一个只读事务内复核 resource/canonical store/generation、head revision、record count、query/index maxima binding，并以既有 `tm_record` 与 `(gram_size, gram, record_id)` 索引取得全部长度 `m/n` 与精确 bigram 交集 `I`。令 `Bq=max(m-1,0)`、`Br=max(n-1,0)`、`L=max(m,n)`、`C+=min(m,n)`、`d1=max(abs(m-n), L-C+, ceil((Bq+Br-2I)/4))`，据此得到保守 `U1`；单字符特例在字符相等尚未知时必须取 Dice 上界 1.0。phase 1 提交后只按 `U1` 前沿评分足以建立真实第 k 名元组 `K0` 的前缀，禁止用 exact bigram、字符/bigram 分项最小值或其他 component heuristic 提前排除 identity。
- session 自行定义唯一合法精化集 `R = {未计入 r | U1(r) >= threshold 或 (U1(r), record_id(r)) >= K0}`。phase 2 必须重新绑定同一 resource/store/generation/head/count/query/index facts，在一个短只读事务内仅为严格有序的 `R` 返回精确字符 multiset 交集 `C`，拒绝缺失、重复、乱序、越界或额外 identity；提交后以 `d2=max(abs(m-n), L-C, ceil((Bq+Br-2I)/4))` 得到 `U2` 并继续真实 scorer-v1。必须以穷举/随机对照证明 `真实分数 <= U2 <= U1`。两个事务均不得跨 scorer callback；phase 2 前、phase 2 中/后及 scorer 期间的 append/head 漂移分别由 phase binding 或最终 generation/head 复核稳定 fail-closed。
- 两阶段证明冻结 `A0`（精化前已计入）、`P1`（U1 安全排除）、`R`、`A1`（R 中后续计入）与 `P2`（U2 安全排除），并强制 `total=A0+P1+R`、`R=A1+P2`、`accounted=A0+A1`、`unscored=P1+P2`。最终 threshold 与 top-k 闭合必须同时证明 `P1` 的最大 U1 前沿和 `P2` 的最大 U2 前沿均被真实 threshold 与最终 kth 元组严格支配；相等仍视为未闭合。公开 metadata 只携带 traversal phase/version、是否精化、各分区计数/最大前沿、精化请求与返回计数及 `K0`，不得泄露 folded text、gram、等价键或正文。
- 对固定 query，只有完整 `fold-v1(source_raw)` 完全相等的 record 才构成 scorer 等价类。TMRetrievalService 必须从 health-validated record 自行重建等价类；每类首次出现运行一次真实 scorer-v1，后续 identity 复用同一不可变 evidence，但各自的 raw source、target、provenance、record id 与稳定 tie 仍独立保留。hash、长度、gram、seed、调用方分组或自报计数均不能建立等价；任意注入 scorer 也不能冒充可复用的 scorer-v1 owner。
- `candidate-budget-v1 = min(8192, max(2048, result_limit * 128))` 保持不变，并只限制单资源为闭合证明执行的真实 scorer-v1 调用次数。`proof-query-v2` 分开冻结 `scorer_invocation_count`、`accounted_identity_count` 与 `unscored_identity_count`：调用数等于已计入 identity 的 exact folded-source 等价类数且不得超过 budget，已计入与未计入 identity 之和必须等于总 record 数；BOUND_PROOF/UNION/DEDUPLICATE 和候选 identity 数按 `accounted_identity_count` 对账，不再把 identity fan-out 误算为额外 scorer 调用。预算耗尽而证明未闭合时返回稳定的资源级 `CANDIDATE.PROOF_BUDGET_EXHAUSTED`。
- 每次查询按执行顺序记录 FTS_TRIGRAM/GRAM_3/GRAM_2/GRAM_1 seed、BOUND_PROOF、UNION、DEDUPLICATE 与可选 TRUNCATE 的守恒事实；同时冻结 traversal mode/version、总 block/record 数、扫描/打开 block、已检查上界、scorer 调用、已计入/未计入 identity、未评分最大上界、阈值与第 k 名闭合事实。未执行阶段不伪造零计数，proof inspected、scorer invocation、accounted identity 与最终 global returned count 不得互相冒充。只有“所有未计入 identity 的上界均低于最低相似度”且“任一未计入 `(upper_bound, record_id)` 都不能超过当前真实第 k 名 `(score, record_id)`”同时成立，候选证明才闭合。
- CandidateRetriever 与 TMRetrievalService 通过私有 proof port 交替推进，公开 `CandidateRetrievalReport` 仍只暴露候选身份与 frozen `CandidateRecallMetadata`；评分 evidence 由 Retrieval 持有并直接用于 threshold、稳定排序和跨资源 global limit，不重复评分、不允许 candidate owner 授予 capability。
- query report 与 benchmark 复用同一 proof-aware recall metadata contract；阶段计数、union unique、dedupe、truncate、scorer invocation、accounted identity、returned 必须可对账，任何负数、顺序错乱、上界低估、`scorer_invocation_count > candidate_budget`、等价类/identity 守恒错误或未闭合证明都是 validation failure。fuzzy gate 未过时 recall metadata 明确返回 unavailable code 与空阶段/候选。
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

`resources` 可包含完整 ResourceConfig adapter 集合；Retrieval 只为 `active=true && lookup=true` 的 handle 获得 store lease。每个参与查询的资源只取得一次只读 query lease，`StoreHealth`、exact/context 分类、candidate recall 和 candidate record 批量读取全部消费同一个 generation view；任一步失败都丢弃该资源的局部结果并关闭 view，不能用新的 lease 拼接同一份资源报告。`TMQuery.resource_order` 必须与 handle ids 一一对应并决定跨资源 tie order。Legacy facade 的 save path 独立只写 `active=true && update=true` handles；Lookup 不授予写权限，Update 不授予查询权限。

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

### Retrieval capability publication

`tm_retrieval_capability.py` 是 retrieval gate 的唯一判定与发布边界。它只依赖 frozen contracts 和不可变 validation evidence，不导入 store、candidate、retrieval 或 benchmark runner；`tm_sqlite_store.py`、`tm_candidate_index.py` 和 `tm_benchmark.py` 也不得反向成为能力判定权威。`SQLiteTMStore.health()` 只报告同一 generation 的物理事实和 canonical exact 可用性，CONTEXT/FUZZY 的 query-effective availability 由 Retrieval 在内存中组合，不能写回 DB、coordinator、binding 或 migration report。

Gate C 的固定输入、expected/observed canonical digest 重算和 manifest 生成由 `tm_retrieval_validation.py` 独占；它是离线 validation leaf，可以消费 `tm_retrieval.py` 的纯分类/评分入口、公开 query/store 端口和 `tm_retrieval_capability.py` 的 frozen evidence values，以临时资源重放事务回滚、局部失败和 global-limit cohorts，但任何 production runtime 模块都不得反向导入它。该模块不接触 facade 或 Qt，也不得发布能力。`tm_retrieval_capability.py` 保持 evaluator/publisher 状态机边界，不继续吸收 fixture codec、向量 runner 或测试语料；避免把“如何产生证据”和“谁有权解释/发布证据”重新耦合。

批准 roots 可以覆盖 evaluator/build 文件本身，因此不得把这些文件的 observed digest 回填为被哈希生产模块中的默认常量，否则会形成自引用身份。无 evidence 的默认 publisher 始终保持 fail-closed；离线验证只返回从批准 roots 构造的不可变 expectation 与 manifest，外层 composition root 再用这两个值显式构造 `RetrievalCapabilityEvaluator`/`RetrievalCapabilityPublisher` 并注入 Retrieval。该装配不使 validation leaf 成为发布者，runtime 也不读取 roots；任何默认常量、调用方布尔值或未闭合 manifest 都不能替代批准 roots。

为重放 single-snapshot、局部失败和 global-limit 固定服务 cohort，validation leaf 可以在函数内部用批准 expectation 与已独立重算通过的 CONTEXT evidence 构造不返回、不持久化的 harness-scoped evaluator/publisher；该值只为本次固定输入提供执行视图，不成为 production composition root。harness 必须让 fuzzy-core 与 Gate D 保持关闭，完整 service transcript 产生后才计算 observed fuzzy-core digest；最终 digest 或 manifest 不得反向授权生成它的同一次执行，避免 evidence 自举。

`RetrievalCapabilitySnapshot` 至少冻结 retrieval semantics version、CONTEXT 子门决定、fuzzy-core correctness 决定、`FTS5_TRIGRAM` 与 `GRAM_FALLBACK` 两条 Gate D 决定，以及只含版本、digest、时间和稳定 unavailable code 的不透明 evidence summary。CONTEXT、fuzzy-core 和两条 benchmark path 可分别降级，任何一项不得替另一项宣称成功。FUZZY 对某次查询可用，当且仅当 fuzzy-core correctness 与该查询实际执行路径的 Gate D 都开放；Task 7.5 完成但 Task 8 尚未发布 benchmark evidence 时，FUZZY 必须继续关闭。

`RetrievalCapabilityEvaluator` 是 evidence 到决定的唯一函数；manifest 中的自报 `passed` 不能单独授予能力。evaluator 必须重新核对批准的 cohort/fixture/build/semantics/evaluator digest、有效期和可重算结果；Gate D 还要核对 frozen benchmark contract、execution path、environment/report digest 和 hard-gate 结果。`RetrievalCapabilityPublisher` 只接受精确 evaluator/manifest 值，构造时私有克隆 expectation，refresh 时在锁内重新求值并原子替换整个 snapshot；调用方不能注入返回任意 `available=True` 的 callback。缺失、过期、版本/digest 不符或重算失败都 fail-closed，且允许从 open 降级为 closed。

`TMRetrievalService.query()` 在读取任何资源前只取得一次 capability snapshot，并让同一不可变值服务整次多资源查询；发布者随后 refresh 不改变在途 query。每个资源仍只取得一次 generation view。Retrieval 先复核 physical health/exact/generation，再把 snapshot 与查询的 intended recall path 组合为 query-effective health：仅当 physical `index_kind` 是 `FTS5_TRIGRAM` 且 fold-v1 query 长度至少为 3 时选择 fast path，否则选择 `GRAM_FALLBACK`。对应 FUZZY 子门关闭时不得读取 CandidateRetriever、candidate records 或 scorer，而是返回带 intended path、空阶段和 evaluator stable unavailable code 的 recall metadata；开放后 CandidateRetriever 返回的 `index_kind` 必须与 intended path 一致，否则该资源 fail-closed。

CONTEXT 关闭时仍保留 exact winner，但不返回其他 same-source variants；FUZZY 关闭时不影响 exact、已开放 CONTEXT 或 save。能力开放但零命中时 availability 为 true 且 unavailable code 为空；门关闭时 availability 为 false、结果和阶段为空且 code 非空。稳定 code 由 evaluator 按“identity/version/digest 不符 → evidence 缺失 → evidence 重算失败 → evidence 过期”的固定优先级产生，分别使用 `RETRIEVAL.CONTEXT_*`、`RETRIEVAL.FUZZY_CORRECTNESS_*` 和 `RETRIEVAL.FUZZY_BENCHMARK_*` 命名空间，不再由 store 硬编码 `STORE.*_GATE_CLOSED`。

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

facade 每次进程级打开都必须重建同一权威判定，不得把“本进程内已经持有 canonical handle”当作唯一成功条件：

| 冷启动可观察状态 | facade 权威 | 必须保持的语义 |
|------------------|-------------|--------------------|
| 从未完成首次激活，或首次 `PREPARED` 已可证取消且无 canonical generation | legacy JSONL | 只有这两种状态可以使用 JSONL runtime |
| completed binding 指向当前 head | canonical / `VERIFIED_CURRENT` | 恢复唯一 generation，exact/save 不回退 JSONL |
| 合法 append 或 merge import 使 head 超过 completed binding | canonical / `VERIFIED_HISTORY` | 冷重开仍必须成功；历史 snapshot 不是激活损坏 |
| 配置 JSONL/manifest 与 ledger 不再一致 | canonical / `SOURCE_DIVERGED` | 冷重开后 Lookup/Update 继续，divergence 保持锁存，不修改 JSONL |
| activation 未闭合、canonical 资产损坏或身份歧义 | coordinator recovery 或 fail-stop | 不得因 canonical 不可用而猜测回退 JSONL |

冷打开先恢复 canonical generation/lineage，再由 `SourceBindingMonitor` 对 completed binding 派生 CURRENT、HISTORY 或 DIVERGED；不得反过来要求 binding revision 必须等于 head 才允许恢复 generation。

### TMBenchmark

```python
class TMBenchmark:
    def run(self, contract: BenchmarkContract) -> BenchmarkReport: ...
```

`TMBenchmark` 是最终组合入口，不是要求把全部 benchmark 逻辑堆入一个文件。Task 8.1 的确定性语料与 digest 权威保留在 `tm_benchmark.py`；Task 8.2、8.3、8.4 分别由 latency、process/RSS、oracle owner 产生不可变原始证据；Task 8.5 的 `tm_benchmark_query_process.py` 只把已验证的迁移 artifact 重开为真实查询进程并产生 latency/RSS 执行证据，`tm_benchmark_gate.py` 只组合这些证据、构造两个独立路径报告并发布 Gate D。前三个执行 owner 和 query-process bridge 不得构造最终 `BenchmarkReport` 或授予 capability，gate owner 不得重新选择 cohort、丢弃原始样本或重写 oracle 结果。这些 owner seam 只分隔独立故障模型，Cluster J 仍在 8.1–8.5 全部闭合后做一次累积复审和一次 fresh full suite。

Task 8.3 的迁移 child 在专用 run root 内完成激活、reopen 与健康验证后退出，不把进程内 `SQLiteTMStore` handle 伪装成可跨进程复用资产。Task 8.5 为每条路径保持该专用 root 到查询取证完成；query child 在首次查询前根据 process evidence 的 contract/corpus/fixture/resource/store/generation/path 事实重建 deterministic locator，对 canonical sidecar 执行 no-follow regular/single-link identity、digest、fresh coordinator rehydrate、health/index/count 成套复核，然后在同一子进程和同一 generation 上完成全部 warmup 与 measured query。查询前后 artifact identity/digest 必须一致；任一事实漂移都废弃该路径证据，不重新迁移、不改用另一路径。

Task 8.8 的性能修正不删除迁移口径中的任何阶段。fresh mutable stage 只在新建路径执行一次完整语义校验；既有 stage 的 reuse validator 不再重复校验刚构建的同一对象。StageSealer 在一个 `BEGIN IMMEDIATE` 边界内流式完成 exact parity、schema/index/fold/count 与 logical closure 校验并写入 `SEALED` marker，随后 close/fsync，生成 registry-owned `SealedContentAttestation`。该 attestation 绑定 DB/manifest/source 的 SHA-256、device/inode、schema/index/fold version、counts、exact-parity 与 closure digest。

激活前和 lease drain 后的两次 Gate B 均保留；每次必须经 no-follow regular/single-link pre/post identity capture 重新计算完整文件 SHA-256 并与 sealed attestation 比较，但不重复展开 record/index 语义扫描。replace 后须证明 canonical inode/digest 等于 sealed attestation，并执行 reopen、schema、integrity 与 foreign-key 校验。合法 receipt/meta 激活写入后执行一次 active-set 全语义校验并生成 `ActiveContentAttestation`；之后的 manifest/generation/final closure 只可在 exact byte/inode 与 phase facts 均匹配该 attestation 时复用。active attestation 持久进入 journal/terminal，cold recovery 重新 hash、reopen 并核对；缺失、损坏、过期或身份漂移继续按原恢复矩阵 rollback/fail-stop。四个 journal phase、两次 Gate B、parent fsync、replace、reopen 与 last-known-good 语义均不改变。

query child 中的 latency executor 必须调用生产 exact 和 `fold-v1 → seed + bound-proof batches ↔ scorer-v1 → threshold → stable top-k` 链路，并由实际 store health/candidate/proof metadata 回显 execution path；不得以 synthetic callback、仅候选身份、oracle identity 或调用方自报 path 代替。两条路径可以共享 proof closure 算法，但必须分别执行各自 seed/index path 并发布独立报告。迁移 child 与 query child 分别采样峰值 RSS，路径报告使用两个独立进程的较大值；迁移耗时仍只取 Task 8.3 已冻结的全生命周期口径。

最终 machine-readable evidence bundle 保留 latency 的全部原始样本、process/query/oracle 的不可变事实与 digest，以及 strict `BenchmarkReport`/`BenchmarkSuiteReport` codec 结果。本地 child protocol 可以使用经严格验证的绝对路径定位本次临时资产，但可移植 bundle 只保留由 contract/corpus/path/artifact/evidence digest 构成的稳定 artifact key 与必要环境事实；不发布 run-root/fixture 绝对路径、PID 或可跨机器误用的句柄。专用 root 只在 bundle 原子落盘并严格回读后由调用方在测量外整体回收；Gate D 只消费已回读的 bundle，不直接信任临时路径或运行中对象。

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
| Export | fsync/replace 失败 | `ExportFailure` 报告旧目标保持证据、recovery locators 与 committed/ambiguous 状态，无法证明时 fail-stop |
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
- candidate proof 对稀疏 frontier 以 block-level 保守上界 best-first 打开小批量 record；当保守 maxima 退化为近全量 block 时，切换为单事务 set-based exact-bound scan，禁止用每 block 一次连接/事务/count 复证实现同一密集扫描。两种模式共享 exact record bound、fold-equivalence、budget 与闭合语义，上界只能多取，不能漏取。
- migration 先 bulk insert、后建大索引；transaction 与 RSS 由 100k gate 约束。
- FTS5/gram seed 只控制 execution path 与首批 proof 队列；完备性由共同的 versioned bound proof 决定，默认 scorer budget 写入 versioned contract。
- sealed/active attestation 只消除同一 immutable byte identity 上的重复语义扫描；任何字节、inode、phase 或 durable evidence 漂移都重新进入完整验证或 fail-stop，不能以 stat/mtime/size 或缓存布尔值代替。
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
- snapshot publication/recovery 的 mutation-proof 矩阵覆盖 ancestor/direct-parent rename/ABA、symlink/hardlink/multi-link、source/destination 在最后复证后被同字节或异字节 inode 替换、每个 fsync/replace/completion/cleanup 边界的进程死亡、durable temp/handoff 缺失或损坏、terminal replay 幂等和外来 inode 不删不覆盖。
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
