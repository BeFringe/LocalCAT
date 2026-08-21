# 需求文档

## 简介

LocalCAT 的 canonical TM 已经具备 SQLite 持久化、generation lease、source binding、snapshot recovery 与有界 fuzzy candidate proof，但 `tm_sqlite_store.py` 仍同时容纳 candidate 合同、算法所依赖的查询面、recall/proof SQL 及 candidate projection 写入数据面。`tm_candidate_index.py` 因此直接导入具体 `SQLiteTMStore` / `SQLiteTMQueryView` 与 store 私有 validator，扩大了 scorer/proof algorithm、SQLite 实现与 Gate C/D evidence 的共同变更面。

本规格只做行为保持型维护：建立中立 candidate storage contract/port，把 candidate recall/proof/projection SQL 数据面迁入独立 SQLite owner，并让 candidate algorithm 不再依赖具体 store 类。所有现有 schema、generation、transaction、candidate ordering、proof-query-v3、Gate C/D 与用户可见行为必须原样保留。

## 边界说明

- **范围内**：candidate leaf DTO/protocol/error/constants；candidate algorithm 对中立 port 的消费；recall/proof/projection SQLite SQL 数据面；store/query-view 兼容入口；late-bound fault seam；FTS5/fallback 双路等价；Gate C/D、fault/acceptance/release current-source evidence 重验。
- **范围外**：scorer、candidate budget、proof-query-v3 语义、threshold/top-k、schema/version/migration；coordinator/generation/activation/reattestation；source binding；snapshot ledger/recovery；`tm_contracts.py` 拆分；Parser/Qt/多文档/chunk/sync；以行数为目标的清理。
- **相邻期望**：Integration TM evidence owner 消费本规格的 current-source roots 与 Gate C/D 产物；Multi-Document 不依赖本维护线；资源可移植性/同步只从已批准 canonical TM 导入导出面消费资源，不直接复制 live SQLite。

### Scope Lineage

- **Owning spec**：`tm-store-module-extraction`
- **被修订的既有范围说明**：`tm-storage-retrieval-index` Design/Tasks 5.R1–5 已批准 activation、schema-upgrade 与 snapshot artifact 三轮提取；本规格仅继续 candidate storage 责任分离，不重开 Feature 5 功能语义。
- **相邻规格 / 契约**：`tm-storage-retrieval-index`、`feature5-ui-integration`、ADR-007/008/009/010/011/013/016。
- **审批状态**：用户于 2026-08-21 批准按“TM Store 治理 → TM Store 解耦”推进，并批准支付完整 Gate C/D 成本。

## 需求

### Requirement 1：行为与持久契约等价

**目标：** 作为 LocalCAT 维护者，我希望模块拆分不改变任何 TM 业务事实，以便把风险限定在代码责任边界。

#### 验收标准

1. 系统应保持 schema v2、schema digest、candidate table/index/FTS5 对象、meta key 与持久 JSON/SQLite 格式完全不变。
2. 当对同一 canonical snapshot、generation、query、threshold、top-k 与 candidate budget 查询时，系统应产生与基线完全相同的 recall/proof/order/result/error code。
3. 当 append、streamed import、activation、schema upgrade、snapshot refresh/recovery 或 source binding 运行时，系统应保持事务、generation、receipt、fault order 与磁盘效果。
4. 若新模块无法保持任一已冻结行为，解耦应 fail closed，且不得以更改 Feature 5 契约的方式消除失败。

### Requirement 2：Candidate 叶合同与窄 Port

**目标：** 作为 candidate algorithm 维护者，我希望只消费稳定的 candidate contract/port，以便不必了解 SQLite store 内部状态机。

#### 验收标准

1. 系统应建立不导入 store、retrieval、Engine、Controller 或 Qt 的 leaf candidate contract 模块。
2. Leaf contract 应独占 candidate input/write-plan/recall/proof/dense-phase DTO、candidate proof index error、version/block constants 与读取 port protocol。
3. DTO 应继续使用 exact built-in type、frozen dataclass、tuple、bounded/ordered identity 与现有 invariant，不宽化 forged/subclass 输入。
4. Port 应只表达 recall/proof 所需的读取行为与 resource identity，不暴露 connection、transaction、coordinator、lease token 或 publication authority。
5. 系统应保留 `tm_sqlite_store` 对既有公开 candidate 名称的同一 class/function object 兼容导出，直到本规格的退役门明确证明无消费者。

### Requirement 3：Candidate Algorithm 脱离具体 Store

**目标：** 作为 retrieval 维护者，我希望 candidate proof algorithm 只通过 port 调用 storage，以便 scorer/proof 与 SQLite 实现可分别审计。

#### 验收标准

1. `tm_candidate_index.py` 不得导入 `tm_sqlite_store`、`SQLiteTMStore`、`SQLiteTMQueryView` 或 store 私有 validator。
2. 当 candidate recall 或 proof session 建立时，algorithm 应验证中立 port 的行为面、resource identity 与返回 DTO，并保留现有 fail-closed code。
3. Algorithm 应继续独占 candidate budget、stage union、frontier、U1/U2/U3/U4、P1/P2/P3、scorer invocation 与 threshold/top-k completion 语义。
4. 解耦不得将 scorer、similarity、candidate report 或 UI payload 移入 storage contract/data plane。

### Requirement 4：SQLite Candidate 读数据面

**目标：** 作为 SQLite store 维护者，我希望 recall/proof SQL 只有一个数据面 owner，以便 store/query-view 只编排 lifetime 和 transaction。

#### 验收标准

1. SQLite candidate data-plane 模块应拥有 FTS5/fallback recall、seed stages、block/projection proof、dense phase 1/2 与 proof-index validation 的 SQL/row decoding。
2. Data plane 应只消费调用方提供的 `sqlite3.Connection`、不可变 generation/store identity facts 与 leaf DTO，不自行打开 canonical authority。
3. `SQLiteTMStore` / `SQLiteTMQueryView` 兼容入口应在现有 lease/lifetime 内打开 connection、BEGIN/COMMIT/ROLLBACK、验证 generation/head/count，然后调用数据面。
4. Data plane 不得发布 generation、capability、receipt 或 source binding，也不得拥有跨 owner transaction completion。
5. 当 SQLite 或证明事实异常时，兼容入口应继续映射同一 `STORE.CANDIDATE_*` stable code 并回滚。

### Requirement 5：SQLite Candidate 写入/Projection 数据面

**目标：** 作为 TM import 维护者，我希望 candidate rows 与 canonical records 仍在同一 store-owned transaction 中写入，以便不产生部分索引。

#### 验收标准

1. Data plane 应拥有 gram/FTS rows、candidate block summaries、gram block maxima、projection digest 与 streamed candidate-index build 的 SQL 数据面。
2. 当 append batch 或 streamed batch 写入时，store 应先验证 complete candidate write plan，然后在现有 canonical transaction/chunk transaction 中调用数据面。
3. 若 extension、candidate SQL、summary、commit 或任一 chunk 失败，store 应保持现有 rollback/batch status/head revision 语义与旧字节。
4. Data plane 不得持有 extension callback、caller frame、coordinator 或 transaction completion authority。
5. FTS5 与 fallback 应使用相同的 deterministic write-plan/proof facts，不改变现有 candidate index version。

### Requirement 6：兼容入口、Generation 与事务权威保留

**目标：** 作为 lifecycle 维护者，我希望所有授权状态机留在原 owner，以便模块拆分不变成 authority 迁移。

#### 验收标准

1. `ResourceStoreCoordinator` 应继续独占 lease/drain/state/generation/activation/token/ticket 状态机。
2. `SQLiteTMQueryView` 应继续绑定 captured generation，并独占 lifetime/expiry/foreign binding 拒绝。
3. `SQLiteTMStore` 应继续作为公开 store/coordinator 入口，独占连接政策、transaction completion、schema/bootstrap、ledger/binding 与 public error mapping。
4. 解耦不得移动 activation/recovery、reattestation、source binding、schema upgrade、snapshot artifact/recovery 或 migration owner。
5. Data plane 不得反向导入 `tm_sqlite_store.py`、`tm_candidate_index.py`、retrieval、Engine 或 Application。

### Requirement 7：兼容导入与 Fault Seam

**目标：** 作为故障注入测试维护者，我希望移动后仍能精确触发同一故障点，以便等价性可证明。

#### 验收标准

1. 在移动实现前，系统应冻结 candidate 相关 public/private import、patch target、call order、error code 与 transaction/fault matrix。
2. 当既有 consumer 从 `tm_sqlite_store` 导入 candidate 名称时，compatibility surface 应继续可用且不产生第二实现。
3. 当既有 test 在 `tm_sqlite_store` patch candidate seam 时，store call site 应仍在运行时解析该 late-bound wrapper，故障顺序与 rollback 不得改变。
4. 退役阶段只应移除经 production/tests/evidence 闭合扫描证明无消费者的 wrapper，不得以“新模块已存在”代替退役证据。

### Requirement 8：依赖方向与唯一权威

**目标：** 作为架构评审者，我希望可以机械证明新边界，以便未来变更不会重建循环依赖或平行 SQL 权威。

#### 验收标准

1. Architecture guard 应证明 `tm_candidate_store_contracts` 是叶模块，`tm_candidate_index` 只依赖 candidate contracts 与 TM frozen reports/scorer contracts。
2. Architecture guard 应证明 steady-state recall/proof/write/projection SQL 与 row decoding 仅存在于 `tm_sqlite_candidate_projection`，store 只保留 transaction/lifetime wrapper 与必需 schema DDL；已批准的 schema-copy 与 stage-sealing SQL owner 是独立的迁移/封存权威，不构成第二 operational candidate authority。
3. Architecture guard 应拒绝 data plane 导入 store/candidate algorithm/retrieval/Engine/Application/Qt，以及 candidate algorithm 导入 store/data-plane concrete implementation。
4. 系统应保持 Parser、Multi-Document、Chunk、Sync 与 TM Store 维护线互不导入。

### Requirement 9：Current-source Evidence 与 Gate C/D

**目标：** 作为 capability owner，我希望任何 candidate source-root 改变都重新支付真实证据成本，以便 Fuzzy 不会因为纯重签而被授权。

#### 验收标准

1. 当 candidate/store source roots 变更时，系统应使现有 fault/acceptance/release、Gate C roots 与 Gate D implementation fingerprint 失效。
2. Implementation 应在最终 source roots 上重放 store/lifecycle/activation/binding/snapshot/schema/migration/candidate/retrieval/benchmark 相关 suites。
3. Gate C owner 应把新 candidate contract/data-plane/store/index roots 纳入真实 build/source root 并重算 current-source evidence。
4. Gate D owner 应在同一 final implementation fingerprint 上真实运行 100,000 条 FTS5 与 fallback intended paths，不得复用旧 run receipt 或只重签 bundle。
5. 若 Gate C、Gate D、fault、acceptance 或 release 任一失败，feature 应保持 NO-GO，且 Fuzzy 不得由本规格自行放开。

### Requirement 10：分波交付与主线隔离

**目标：** 作为项目 owner，我希望 TM Store 维护线可审计地交付，以便它不抢跑或污染 Multi-Document 主线。

#### 验收标准

1. 实施应按“characterization → leaf port → SQLite read data plane → write/projection data plane → evidence/retirement”分波提交。
2. 每个 Wave 应在独立提交中附带 focused test、architecture guard、diff check 与明确的兼容 seam 状态。
3. 当本维护线运行时，implementation 不得修改 Multi-Document/Chunk/Sync/ResourcePackage/Parser/Qt 功能范围。
4. Feature 只应在所有 Requirements/Design/Tasks、ADR/Steering、current-source evidence 与退役门闭合后标记 completion。
