# 实施计划

## 任务说明

本计划只实施行为保持型 TM candidate store 解耦。每个 Wave 在共享不变量闭合后形成一次阶段性 Git 提交；不得把下一 Wave 的 production 变更抢跑进当前提交。所有 checkbox 只在当前提交上的 fresh evidence 通过后勾选。

固定顺序：

```text
Wave 0 治理与 characterization
  → Wave 1 叶合同与中立 port
  → Wave 2 SQLite 读数据面
  → Wave 3 SQLite 写/projection 数据面
  → Wave 4 Gate C/D、evidence、退役与 Feature GO
```

## Wave 0：治理与 Characterization

- [x] 1.1 完成中文 Requirements、Research、Design、Tasks 与 spec metadata
  - 冻结 candidate 叶合同、port、SQLite 数据面与保留 owner。
  - 明确 Parser/Multi-Document/Chunk/Sync/Qt/TM contracts 均不在本规格。
  - 明确每 Wave 一次阶段提交、完整 Gate C/D 成本与 NO-GO 条件。
  - _Requirements: 1.1–1.4, 6.1–6.5, 9.1–9.5, 10.1–10.4_

- [x] 1.2 采纳 ADR-017 并建立 review clustering / border
  - ADR-017 只改变 candidate algorithm 与 storage data-plane 的依赖方向，不迁移 canonical authority。
  - `tech.md` 只引用已采纳决策；`structure.md` 等 runtime 文件真实落地后再同步。
  - 冻结 Cluster 0–4 的 review gate 与红线。
  - _Requirements: 6.1–6.5, 8.1–8.4, 10.1–10.4_

- [x] 1.3 建立 candidate import/patch/SQL/transaction characterization
  - 枚举 production/tests/evidence 对 candidate DTO、常量、错误和 private seam 的 import/patch target。
  - 冻结 FTS5/fallback recall、proof block/dense、append/streamed 的 call/fault/error 顺序。
  - 冻结 `tm_sqlite_store.py` 与 `tm_candidate_index.py` 中 candidate SQL owner 基线，禁止迁移后出现第二份实现。
  - _Requirements: 1.1–1.4, 7.1–7.4, 8.1–8.4_

- [x] 1.4 冻结 current-source evidence 与 Gate C/D impact inventory
  - 固定 fault/acceptance/release、Gate C roots、Gate D implementation roots 与 strict evidence consumer。
  - 证明新增 leaf/projection 必须进入真实 roots，旧 wrapper 不能掩盖 fingerprint 变化。
  - 保存当前 100k bundle 只作 stale 基线，不授权后续重签复用。
  - _Requirements: 9.1–9.5_

### Wave 0 完成门

- 1.1–1.4 全部完成；characterization fresh 通过。
- 无 production runtime 变化。
- 独立提交：`docs(tm-store): 冻结 candidate 模块提取治理`。
- Cluster 0 review 通过后才进入 Wave 1。

## Wave 1：叶合同与中立 Port

- [x] 2.1 新建 `tm_candidate_store_contracts.py`
  - 移动 candidate 常量、错误、DTO、opaque dense receipt 与纯 validators/builders，不复制定义。
  - 叶模块不导入 sqlite/store/index/retrieval/Engine/Application/Qt。
  - exact built-in/frozen/tuple/nested invariants 与 stable codes 保持不变。
  - _Requirements: 2.1–2.5, 8.1_

- [x] 2.2 保留 `tm_sqlite_store` 同一对象兼容导出
  - 既有 candidate class/function/constant 名称继续可导入。
  - 对每个名称证明 `tm_sqlite_store.X is tm_candidate_store_contracts.X`。
  - 不创建 compatibility subclass、复制 dataclass 或平行 validator。
  - _Requirements: 2.5, 7.1–7.4_

- [x] 2.3 让 `tm_candidate_index.py` 只消费中立 port
  - 移除 concrete `SQLiteTMStore` / `SQLiteTMQueryView` import 与 exact-type 准入。
  - 用 `CandidateRecallPort` / `CandidateProofPort` 验证行为、resource identity 和返回 DTO。
  - 保持 budget/stage/frontier/scorer/threshold/top-k algorithm owner 不变。
  - _Requirements: 3.1–3.4_

- [x] 2.4 建立 dependency/object-identity/forgery guards
  - 真实 production tree 证明 leaf、algorithm、store 依赖方向。
  - hostile port、forged nested DTO、resource mismatch 在首次 storage/scorer 前拒绝。
  - FTS5/fallback 与 proof report exact equivalence fresh 通过。
  - _Requirements: 2.1–2.5, 3.1–3.4, 8.1, 8.3–8.4_

### Wave 1 完成门

- SQL owner 尚未移动；所有现有 SQL 仍只有 store 一份。
- candidate algorithm 不再导入 concrete store/query-view。
- 独立提交：`refactor(tm): 建立 candidate storage 中立 port`。
- Cluster 1 cumulative review 通过后进入 Wave 2。

## Wave 2：SQLite Candidate 读数据面

- [x] 3.1 提取 recall / FTS5 / fallback 查询数据面
  - 移动 recall input SQL、stage rows、folded-source decode 到 `tm_sqlite_candidate_projection.py`。
  - store/view 保留 lifetime、connection、read transaction、identity/head/count 与 error mapping。
  - FTS5 unavailable/degenerate、GRAM_1/2/3、ordering/cap exact 等价。
  - _Requirements: 1.1–1.4, 4.1–4.5_

- [x] 3.2 提取 proof snapshot / block / maxima 数据面
  - 移动 seed stages、block/maxima digest、sparse records SQL 与 row decoding。
  - generation/head/count 的授权判断仍由 store 入口完成。
  - stale/invalid/query-failed stable code 与 rollback 保持。
  - _Requirements: 4.1–4.5, 6.1–6.5_

- [x] 3.3 提取 dense phase 1/2 数据面
  - 移动 length/bigram 与 ordered folded-source projection SQL。
  - 保持 opaque receipt/binding、U1/U2/U3/U4 与 P1/P2/P3 不变量。
  - dense/sparse/frontier/scorer invocation exact equivalence。
  - _Requirements: 3.3–3.4, 4.1–4.5_

- [x] 3.4 保留 read fault seams 与 closed-world SQL owner
  - 既有 store private patch target继续 late-bound 委托 projection。
  - projection 禁止 connect/commit/rollback/coordinator/import store。
  - AST/mutation test 证明 recall/proof SQL 只在 projection 存在。
  - _Requirements: 7.1–7.4, 8.1–8.4_

### Wave 2 完成门

- read wrapper + projection 的 transaction/lifetime 边界通过 fault matrix。
- FTS5/fallback、sparse/dense、stale generation 全部 fresh 通过。
- 独立提交：`refactor(tm): 提取 SQLite candidate 读数据面`。
- Cluster 2 review 通过后进入 Wave 3。

## Wave 3：SQLite Candidate 写入 / Projection 数据面

- [x] 4.1 提取 write-plan apply 与 candidate row projection
  - 移动 gram/FTS insert、block summary/maxima 与 projection digest SQL。
  - store 先完整验证/私有复制 write plan，再在既有 canonical transaction 中调用。
  - _Requirements: 5.1–5.5, 6.1–6.5_

- [x] 4.2 提取 proof-index validation 与 streamed index build
  - 移动 index recomputation、digest validation、secondary-index suspend/restore/build SQL。
  - chunk transaction、head/batch publication 与 completion authority 留在 store。
  - _Requirements: 5.1–5.5, 6.1–6.5_

- [x] 4.3 闭合 append/streamed transaction fault matrix
  - extension、plan、SQL、summary、chunk、commit 每个 fault 均保持旧 head/count/batch/bytes。
  - patch target仍被调用一次且参数/call order exact；programmer fault 不被吞。
  - activation、schema upgrade、snapshot、source binding、migration 邻接无回归。
  - _Requirements: 1.1–1.4, 5.1–5.5, 7.1–7.4_

- [x] 4.4 闭合 write SQL 唯一 owner 与 authority guard
  - store 只保留 schema DDL、transaction wrapper 与 late-bound delegate。
  - projection 不发布 generation/capability/receipt/binding，不拥有 transaction completion。
  - _Requirements: 6.1–6.5, 8.1–8.4_

### Wave 3 完成门

- append/streamed success/failure 的磁盘与状态等价。
- candidate projection SQL 只有一个 owner。
- 独立提交：`refactor(tm): 提取 SQLite candidate 写数据面`。
- Cluster 3 review 通过后进入 Wave 4。

## Wave 4：Final roots、Evidence、Gate C/D 与 GO

- [x] 5.1 完成 wrapper retirement 并冻结 final runtime roots
  - 只移除 closed production/tests/evidence scan 证明无 consumer 的 wrapper；其余 compatibility seam继续 late-bound 委托唯一实现。
  - 完成最终 dependency/SQL-owner/patch-seam architecture cleanup；此后不得再修改 production runtime。
  - 在 retirement 后重跑 scoped characterization/fault tests，证明没有第二 SQL authority、consumer 或 patch target 漂移。
  - _Requirements: 7.1–7.4, 8.1–8.4_

- [x] 5.2 更新并冻结全部 evidence roots
  - Gate C artifact/build roots 明确纳入 `tm_candidate_store_contracts.py`、`tm_sqlite_candidate_projection.py`、`tm_sqlite_store.py`、`tm_candidate_index.py`。
  - `BENCHMARK_IMPLEMENTATION_SOURCE_PATHS` 同时纳入上述四个实现 root；fault/acceptance matrix source registry按真实行为与故障影响纳入 leaf/projection/store/index，release继续消费两份新 matrix digest。
  - 完成本任务后锁定 production、Gate C roots、benchmark roots 与 fault/acceptance registries；后续任何变化均使之后 evidence stale。
  - _Requirements: 9.1–9.5_

- [x] 5.3 在冻结后的 final roots 上重放全量矩阵并真实重算 Gate C
  - store/lifecycle/activation/reattestation/binding/snapshot/schema/migration/stage sealing。
  - candidate index/proof/query/retrieval/oracle/process/fault suites。
  - 重跑 Gate C，不手改 digest，不沿用旧 roots；无新增失败，既有外部噪声单列且签名不变。
  - _Requirements: 1.1–1.4, 9.1–9.5_

- [x] 5.4 在同一 final fingerprint 上真实运行 Gate D 100k 双路径
  - FTS5 与 fallback intended paths 全部真实运行。
  - scorer/budget/corpus/cohort/seed/threshold/hard gates 不变。
  - 任一路径失败即 NO-GO，不发布 Fuzzy。
  - _Requirements: 9.1–9.5_

- [x] 5.5 重新生成 fault / acceptance / release evidence并完成治理收尾
  - owner 工具生成，strict 消费者复读；不得手改摘要。
  - fault/acceptance 使用 5.2 冻结的 source registry；release 消费两份新 digest，且只有 Gate C/D 与全部 current-source matrix通过后才可 GO。
  - strict 重读 fault/acceptance/release/Gate C/Gate D，证明它们绑定 5.2–5.4 的同一 final runtime fingerprint。
  - 此任务只可再修改 evidence payload、Tasks/Steering/border 等不属于上述 source roots 的治理文件；不得删除 wrapper或修改 production/test registry。
  - 同步 `structure.md` 真实文件表与 `tech.md` 最终事实，归档 border并标 Feature GO。
  - 对最终 diff 重做五类治理语义门；Multi-Document 等相邻线无越界。
  - _Requirements: 9.1–9.5, 10.1–10.4_

### Wave 4 完成门

- 全部 current-source evidence、Gate C/D 和 release GO 绑定同一 final runtime tree；Gate C 之后没有 production 或 source-registry 变化。
- 独立提交：`test(tm): 重签 candidate 提取 current-source 证据`；之后只允许一个不改变任何 evidence root 的 governance completion 提交。
- Cluster 4 final review 通过后才可勾 Feature completion。

## 明确禁止

- 不在任一 Wave 修改 scorer/budget/proof-query/schema/generation/capability。
- 不以 wrapper 保留为理由留下第二份 SQL/validator authority。
- 不从旧 evidence 或相同数字推断 Gate C/D 仍有效。
- 不修改 Parser、Qt、Multi-Document、Chunk、Sync、ResourcePackage 或 TMX interchange。
- 不把 `tm_contracts.py` 拆分、schema/bootstrap 或 completed-authority rehydration夹带进本规格。
