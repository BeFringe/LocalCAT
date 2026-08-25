# Brief: tm-store-module-extraction

> **状态：已提升为正式 Spec。** 本文保留 discovery 时的候选范围与阶段成本；正式实施权威为同目录已批准的 Requirements、Design、Tasks 及 ADR-017。该维护线不得阻塞 Parser、Multi-Document、Chunk 或同步主线。

## Problem

`tm_sqlite_store.py` 已增长到约 1.3 万行，同时承载 candidate projection SQL、query/proof DTO、schema/bootstrap、completed-authority proof、coordinator、generation lease、source binding、snapshot ledger 与 recovery transaction。文件体量已经妨碍职责识别，但其中多数状态机共同认证 generation、receipt 和持久恢复，不能把“文件过大”直接等同于“都可拆分”。

当前最清晰的耦合是 `tm_candidate_index.py` 直接依赖具体 `SQLiteTMStore`、`SQLiteTMQueryView`、candidate DTO 和私有 validator。candidate algorithm 尚未通过窄 port 与 SQLite projection owner 解耦；这会扩大检索算法、存储实现和 Gate C/D evidence 的共同变更面。

## Current State

Feature 5 已完成并批准三轮行为保持型提取：

- R1 将 activation journal/terminal codec、durable file protocol 和 completion/rollback 移出 store；
- R2 将 schema v1→v2 copy 数据面、backup/locator 持久协议和 strict locator proof 移出 store；
- R3 将 snapshot artifact namespace、no-follow proof、copy/replace/cleanup 和 handoff codec 移出 store。

这些模块化边界已由 `tm-storage-retrieval-index` 的 Design/Tasks 与 current-source evidence 证明，不因 store 仍大而重新打开。store 中的大量 late-bound/private re-export 主要保护现有 fault injection seam，也不能在同一补丁中无证据删除。

`tm_contracts.py` 也已膨胀并混合 frozen contracts、benchmark/migration/lifecycle outcomes 与 strict JSON codecs，但它属于 Gate A frozen contract root；本维护线不同时拆它。若未来需要，另开 `tm-contract-codec-extraction` 调研。

## Desired Outcome

在不改变任何用户可见 TM 行为、排序、schema、generation 或 capability 的前提下，先建立 candidate storage port，并把 candidate recall/proof/projection SQL 数据面移到独立 SQLite owner 模块。`tm_candidate_index.py` 只消费中立 candidate contract/port；`SQLiteTMStore` 与 generation-bound query view 继续作为公开入口和 lease authority。

首波完成后：

- candidate algorithm 不再导入具体 store/query-view 类或 store 私有 validator；
- candidate DTO/protocol 有唯一 leaf contract authority；
- SQLite candidate projection SQL 有唯一数据面 owner；
- coordinator、generation、activation、snapshot、binding 与 public store 入口不迁权；
- Gate C、Gate D、fault、acceptance 和 release evidence 在真实新 source roots 上全部重新生成。

## Approach

采用行为等价的两步提取：

1. 新建 leaf contract，例如 `tm_candidate_store_contracts.py`，拥有 candidate snapshot/proof DTO、query/projection protocol、candidate errors/constants。
2. 新建 SQLite 数据面模块，例如 `tm_sqlite_candidate_projection.py`，拥有 recall/proof SQL、projection write/digest/index validation 与 streamed candidate-index build。

`SQLiteTMStore` 和 `SQLiteTMQueryView` 保留 lease/generation 入口，并向新数据面注入 caller-owned connection、transaction context 和不可变 generation proof。新模块不得自行打开 authority、发布 generation、改变 capability 或提交跨 owner transaction。

## Scope

- **In**: candidate leaf contracts/ports；candidate recall/proof SQL；projection write/digest/proof-index validation；streamed candidate-index 数据面；兼容 wrapper；import-boundary guards；fault/acceptance/release evidence；真实 FTS5 与 fallback 双路径 Gate C/D。
- **Out**: scorer、candidate budget、proof-query-v3、排序、阈值、schema/version、migration format、coordinator、activation、reattestation、generation publication、source binding、snapshot ledger/recovery、TM contracts 拆分、Parser codec、Qt 与新用户能力。

## Boundary Commitments

- `ResourceStoreCoordinator` 继续独占 lease/drain/state/generation/activation/token/ticket 状态机。
- `SQLiteTMQueryView` 继续绑定 captured generation 并负责 lifetime/expiry 检查。
- `SourceBindingMonitor` 继续独占 CURRENT/HISTORY/DIVERGED 与 divergence latch。
- snapshot receipt classification、terminal replay、ledger/binding SQL transaction 和 divergence 决策不进入 candidate 模块。
- activation journal/recovery、schema-upgrade ticket/locator 与 snapshot artifact owner 不回流 store，也不被本规格重写。
- candidate projection 模块只在调用方提供的 transaction/lease 内执行数据面操作，不拥有 commit、rollback 或 capability publication。
- 所有旧 fault seam 必须先 characterization，再保留 late-bound wrapper 或显式迁移；不能把 patch target 变化冒充行为等价。
- 本规格不修改 frozen error code、schema、candidate ordering、budget、recall/proof 语义或性能门。

## Evidence Cost

任何 `tm_sqlite_store.py`、`tm_candidate_index.py` 或新增 candidate source root 的变化都会使现有 current-source evidence 失效。实施窗口必须一次支付：

- store/lifecycle、activation、source binding、snapshot refresh/recovery、schema upgrade、migration 与 stage sealing suites；
- candidate index/proof/query、retrieval、benchmark/oracle/process suites；
- 新旧模块 import-boundary 与 late-bound fault seam tests；
- `fault_matrix_evidence.json`、`acceptance_matrix_evidence.json`、`release_criteria_evidence.json`；
- `retrieval_gate_c_roots_v1.json` 的真实新 source roots 与重算 Gate C；
- `benchmark_tm_evidence.json` 的实现 fingerprint，以及 100,000 条真实 FTS5/fallback 双路径 Gate D。

新增模块必须进入真实 build/source roots，不能只让旧 wrapper 留在 manifest 以维持摘要。若无法在同一维护窗口完成这些证据，runtime 提取不得开始。

## Out of Boundary

- 不以行数为理由拆 `ResourceStoreCoordinator` 或 snapshot ledger transaction。
- 不同时移动 schema/bootstrap；它是第二候选，且 `_probe_fts5` 有大量 fault patch seam，需独立 cluster。
- 不同时提取 completed-authority rehydration；它是 cold-open/fail-stop 安全边界，需独立 cluster。
- 不同时清理全部 compatibility wrappers；布局迁移与 fault-seam 退役必须分提交验收。
- 不触碰 `tm_contracts.py`；Gate A 合同 codec 拆分另立 Spec。

## Upstream / Downstream

- **Extends**: 已完成的 `tm-storage-retrieval-index`，只做行为保持型维护，不重新打开其 Feature 语义。
- **Upstream**: Feature 5 approved Design、Tasks 5.R1–R3、Gate A/C/D、fault/acceptance/release evidence 与当前代码事实。
- **Adjacent**: Integration TM surface 继续拥有 current-source evidence publication；Parser 只会触发自己的相邻复验，不拥有本维护线。
- **Downstream**: 更窄的 candidate storage port；跨设备资源可移植性/同步前更易审计的 canonical TM 模块边界。

## Promotion Clusters

1. **责任与 Characterization**：冻结 candidate contracts、现有 SQL/排序/proof/fault seam 和真实 source-root evidence 清单。
2. **Port 与兼容入口**：建立 leaf contract/port，使 algorithm 不再依赖具体 store；保留 late-bound compatibility wrappers。
3. **SQLite 数据面提取**：移动 candidate recall/proof/projection SQL，不迁 transaction/generation authority。
4. **证据重验与退役**：完整 Gate C/D、fault/acceptance/release；确认无生产消费者后再移除旧私有 seam。

每簇按 cc-sdd 阶段门推进。项目 owner 已批准完整 Gate C/D 预算及“治理 → 解耦”顺序；runtime 仍须按正式 Tasks 的 Wave/Cluster 完成门推进，不阻塞 Multi-Document。

## Acceptance Anchors

- 同一 canonical snapshot、generation、query 与 candidate budget 得到完全相同的 recall/proof/order/result。
- FTS5 和 fallback 两条路径均通过真实 100,000 条 Gate D；不得只重签旧摘要。
- activation、reattestation、snapshot recovery、source binding、schema upgrade 和 migration 行为无变化。
- `tm_candidate_index.py` 不导入具体 SQLite store/query view 或 store 私有 validator。
- 新 candidate 模块不拥有 connection publication、transaction completion、generation 或 capability。
- 完整 fault/acceptance/release evidence 绑定新的真实 source roots。
