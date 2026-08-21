# Research: tm-store-module-extraction

## 目标与方法

本调研以 `ui-mvp@23adb29` 为 brownfield 基线，只识别 candidate storage 可提取责任、已冻结 fault seam 和 evidence 成本。它不以文件行数推导责任，不重新设计 Feature 5 scorer/proof/schema。

### 进入维护线时的 evidence 状态

Wave 0 characterization 记录的是进入本维护线前的真实状态，而不是把旧 evidence 一律描述为 current：

- Gate D implementation fingerprint 仍为 `2dadef65550cc338b57686961fe15cbe8a49aa04bdd583ea58beb8b5721f0e44`，与既有 100k bundle 相同；
- fault matrix source files 当前无漂移；
- acceptance matrix 已因先于本维护线落地的术语列选择与 macOS 启动修复而 stale，精确漂移路径为 `editor_controller.py`、`qt_editor.py`、`qt_settings_dialog.py`；
- release owner 自身的 source fingerprint 仍为 `9451b258e765d4c32d6560d8509682a8313e5c84d4d37e8ee409df7e6f8c09c0`，但其消费的 acceptance evidence 已 stale，因此旧 `GO` 不能代表当前整棵树的 release 资格；
- 该 stale 状态不是 candidate 解耦造成，也不能在 Wave 0 通过手工重签消除；Wave 4 必须在最终 roots 上与 candidate 变更一起由 evidence owner 真实重放。

因此本维护线的基线不是“所有 release evidence 当前有效”，而是“Gate D/fault 当前、acceptance 已明确 NO-GO”。后续每波不得把 acceptance 的既有漂移误归因于 candidate，也不得用既有 fingerprint 掩盖新 leaf/projection roots 尚未进入 closed set。

调研对象：

- `tm_sqlite_store.py` / `tm_candidate_index.py` 的顶层定义、导入与 private seam；
- `tm_retrieval.py`、`tm_stage_sealer.py`、`tm_benchmark*`、`tm_application_composition.py` 的生产消费面；
- candidate/store/proof/retrieval/lifecycle/fault/acceptance/release 测试和证据 registry；
- ADR-007/008/009/010/011/013/016 与 `tm-storage-retrieval-index` 5.R1–R3 的已批准边界。

## 基线事实

- `tm_sqlite_store.py` 为 12,978 行，`tm_candidate_index.py` 为 2,677 行。
- R1–R3 已分别提取 activation durable protocol、schema-upgrade data plane 与 snapshot artifacts；新维护线不重开这三个 owner。
- 治理前 focused 基线：`test_tm_candidate_index`、`test_tm_candidate_proof_index`、`test_tm_candidate_proof_query`、`test_tm_sqlite_store` 共 149 项通过，同时覆盖 FTS5/fallback、dense/sparse proof、generation stale、append/streamed rollback 和 schema 事实。
- 现有 Gate D 100k 是 retrieval 资格门，不是通用 store 行数上限。本维护线会使 implementation fingerprint 失效，必须真实重跑。

## 当前责任混合

### 合同与纯原语

`tm_sqlite_store.py` 当前拥有：

- `CandidateProofIndexError`；
- `SQLiteCandidateRecord`、`SQLiteGramRow`、`SQLiteCandidateWritePlan`；
- `SQLiteCandidateRecallSnapshot`；
- `SQLiteCandidateProofBlock`、`SQLiteCandidateProofRecord`、`SQLiteCandidateProofSnapshot`；
- `SQLiteCandidateProofDensePhase1/Phase2` 及内部 dense receipt；
- `CANDIDATE_INDEX_VERSION`、`CANDIDATE_PROOF_BLOCK_VERSION_V1`、`CANDIDATE_PROOF_BLOCK_SIZE`；
- exact n-gram/write-plan 纯原语和 dense receipt validator。

`tm_candidate_index.py` 直接导入上述名称，还导入具体 `SQLiteTMStore`、`SQLiteTMQueryView` 和两个 store private dense validator。这是本规格需关闭的主要方向耦合。

### Candidate 读数据面

`tm_sqlite_store.py` 约 8,122–9,300 行形成一个内聚数据面：

- recall input 验证、FTS match/fallback posting SQL、stable row decoding；
- proof snapshot/block layout/seed stages/query maxima digest；
- sparse block records；
- dense phase 1 length/bigram intersection；
- dense phase 2 ordered fold projection；
- final generation/head/count revalidation。

这些函数同时处理 SQL 与 BEGIN/COMMIT/ROLLBACK。提取时必须拆成：兼容入口保留 lease/connection/transaction，数据面只处理 caller-owned transaction 中的 SQL 与 row facts。

### Candidate 写入/Projection 数据面

`tm_sqlite_store.py` 约 9,655–10,818 行拥有：

- write-plan exact validation/copy；
- gram/FTS application；
- block/max summaries 与 projection digest；
- proof-index recomputation/validation；
- prepared/streamed candidate inserts；
- streamed secondary-index suspend/restore 与 final candidate-index build。

`SQLiteTMStore.append_batch` / `append_streamed_batch` 是 transaction owner；这个 owner 不能迁出。可迁出的是 transaction 内的 candidate SQL 函数。

## 保留 Owner

| Owner | 必须保留的权威 |
|---|---|
| `ResourceStoreCoordinator` | state/drain/lease/generation/activation/token/ticket |
| `SQLiteTMStore` | 公开入口、connection policy、transaction completion、schema/bootstrap、ledger/binding |
| `SQLiteTMQueryView` | captured generation、lifetime/expiry、foreign/stale binding 拒绝 |
| `tm_candidate_index` | candidate budget、stage union、proof frontier、scorer invocation、threshold/top-k completion |
| `tm_retrieval` / capability owners | exact/context/fuzzy 管线与 capability publication |
| activation/schema/snapshot modules | 已批准的 R1–R3 数据面与恢复语义 |

## 生产消费与兼容面

- `tm_candidate_index.py` 是 candidate DTO/port 的主要 algorithm consumer。
- `tm_stage_sealer.py` 直接消费 `CandidateProofIndexError` 与 proof-index validator。
- `tm_benchmark_oracle.py`、`tm_retrieval_validation.py`、`tm_retrieval.py`、`tm_engine.py`、`tm_application_composition.py` 继续消费公开 store/query-view 入口；本次不迫使它们导入 SQLite data plane。
- 大量测试从 `tm_sqlite_store` 导入 candidate DTO/constants，并 patch `_apply_candidate_write_plan`、`_validate_candidate_proof_dense_binding`、`_bounded_seed_stages` 等 private seam。首次提取必须 re-export/late-bound wrapper，不能一次全部删除。

## 精确迁移表

| 现有责任 | 目标 owner | 原 owner 保留 |
|---|---|---|
| Candidate DTO/error/version/block constants | `tm_candidate_store_contracts.py` | `tm_sqlite_store` 同一对象 re-export |
| Candidate recall/proof port protocols | `tm_candidate_store_contracts.py` | store/view 实现，不暴露 connection |
| exact n-gram/write-plan/dense receipt validators | leaf contract 的纯原语部分 | store 窄 wrapper，algorithm 直接消费 leaf |
| Recall/seed/proof/block/dense SQL + row decode | `tm_sqlite_candidate_projection.py` | store/view lease+BEGIN/COMMIT/ROLLBACK+error mapping |
| Gram/FTS/block/max/digest/index build SQL | `tm_sqlite_candidate_projection.py` | append/streamed transaction 与 batch/head publication |
| Candidate algorithm 对 concrete store 的类型检查 | neutral runtime-checkable port | production composition 仍只传 SQLite store/view |
| Existing private patch targets | `tm_sqlite_store` late-bound wrappers | 直到 retirement scan 证明无 consumer |

## 被拒绝的扩张

- 按行数拆 coordinator、schema/bootstrap 或 completed-authority rehydration：状态机没有可独立提交的窄 transaction port。
- 同时拆 `tm_contracts.py`：属 Gate A frozen root，会把两个证据门叠加到一个维护窗口。
- 顺便改 proof-query-v3/candidate budget/scorer：这会从模块维护变成 Feature 语义修订。
- 用 mock/summary 代替 Gate D 100k：无法重新授权真实 intended paths。

## 证据影响

必须重签且重跑：

- `fault_matrix_evidence.json`、`acceptance_matrix_evidence.json`、`release_criteria_evidence.json`；
- `tests/fixtures/retrieval_gate_c_roots_v1.json` 与 Gate C；
- `benchmark_tm_evidence.json` implementation roots/fingerprint 与真实 Gate D 100k FTS5/fallback；
- strict evidence/attestation/release 消费者。

新模块必须成为真实 source roots。保留 wrapper 不得用来伪装 implementation root 没有改变。
