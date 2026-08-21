# TM Store Candidate 模块提取设计

## 概述

本设计把 canonical TM 的 candidate storage 责任从 `tm_sqlite_store.py` 中提取为两个窄层级：叶合同/读取 port，以及 SQLite candidate 数据面。目标不是“把 1.3 万行拆小”，而是让 `tm_candidate_index.py` 不再依赖具体 `SQLiteTMStore` / `SQLiteTMQueryView`，同时保持 Feature 5 已批准的 schema、generation、transaction、proof-query-v3、排序和 Gate C/D 完全不变。

提取后的依赖方向为：

```text
tm_candidate_index ──> tm_candidate_store_contracts
                               ↑
tm_sqlite_store ───────────────┤
        │                      │
        └──> tm_sqlite_candidate_projection
                    └─────────> tm_candidate_store_contracts
```

`tm_sqlite_store.py` 继续拥有公开 store/query-view 入口、connection policy、operation lease、transaction completion、generation、schema/bootstrap、ledger/binding 和 stable error mapping。新 projection 模块只在调用方已经建立的 connection/transaction 中执行 SQL 与 row decoding，不自行打开 authority、提交 transaction 或发布任何 capability。

### 设计目标

- 建立不导入 store/retrieval/Engine/Application/Qt 的 candidate 叶合同与行为式 port。
- 让 candidate algorithm 只消费叶合同/port，不再做具体 SQLite 类的 exact-type 准入。
- 让 recall/proof 与 write/projection SQL 各有唯一 SQLite 数据面 owner。
- 保留 `tm_sqlite_store` 既有公开名称为同一对象 re-export，并保留已冻结 late-bound fault seam。
- 按 read 与 write 两波迁移；任一波均保持 caller-owned lease/transaction 与 fail-closed 顺序。
- 在最终 source roots 上重新支付 fault/acceptance/release、Gate C 与真实 100,000 条 Gate D。

### 非目标

- 不改变 scorer、candidate budget、threshold、top-k、proof-query-v3 或 candidate ordering。
- 不改变 schema v2、candidate table/index/FTS5、meta key、digest 或 migration format。
- 不提取 coordinator、activation/recovery、reattestation、source binding、schema upgrade、snapshot ledger/artifacts 或 completed-authority rehydration。
- 不拆 `tm_contracts.py`，不清理全部 compatibility wrapper，不增加用户可见 UI。
- 不触碰 Parser、Multi-Document、Chunk、Sync、ResourcePackage 或 TMX interchange。

## Boundary Commitments（边界承诺）

### 本规格拥有

- `tm_candidate_store_contracts.py` 的 candidate DTO、常量、错误与读取 port。
- `tm_sqlite_candidate_projection.py` 的 candidate recall/proof/projection SQL 数据面。
- `tm_candidate_index.py` 从 concrete store 到中立 port 的依赖迁移。
- `tm_sqlite_store.py` 的兼容 re-export、transaction/lifetime wrapper 与 late-bound seam 委托。
- 上述 source-root 变化触发的 Gate C/D 与 current-source evidence 重验。

### 保留在既有 owner

| Owner | 保留权威 |
|---|---|
| `ResourceStoreCoordinator` | state/drain/operation lease/generation/activation/token/ticket |
| `SQLiteTMStore` | 公开入口、连接策略、BEGIN/COMMIT/ROLLBACK、schema/bootstrap、ledger/binding、错误映射 |
| `SQLiteTMQueryView` | captured generation、lifetime/expiry、foreign/stale binding 拒绝 |
| `tm_candidate_index` | budget、stage union、frontier、U1/U2/U3/U4、P1/P2/P3、scorer invocation、threshold/top-k completion |
| `tm_retrieval` / capability owner | EXACT/CONTEXT/FUZZY 管线与 capability publication |
| R1–R3 owner | activation durable protocol、schema-upgrade data plane、snapshot artifact protocol |

### 禁止进入新模块

- coordinator、lease token、connection publication、transaction completion；
- generation/capability/receipt/source binding 的铸造与撤销；
- activation journal、snapshot ledger、schema migration 与 recovery 决策；
- scorer、similarity、candidate report、Qt/Application payload；
- Parser、多文档、chunk、sync 或 provider 类型。

## Governance Impact（治理影响）

- **Applicable Steering**：`structure.md`、`tech.md`、`spec-ownership.md`、`roadmap.md`。
- **Applicable ADRs**：ADR-007/008/009/010/013/016；新增 ADR-017。
- **ADR disposition**：ADR-017 已由项目 owner 授权，冻结 candidate algorithm → neutral port → SQLite data plane 的依赖方向；不取代既有 TM authority ADR。
- **Scope amendment**：不重开 `tm-storage-retrieval-index` 功能语义；只接续其已批准 R1–R3 的行为保持型维护方向。
- **Steering sync**：治理阶段只在 `tech.md` 引用 ADR-017；`structure.md` 的真实文件表在 runtime 文件落地且 Feature GO 后同步。
- **Downstream revalidation**：Integration TM evidence owner 或本维护线的同等 owner 必须在最终 roots 上重签 fault/acceptance/release、Gate C 与 Gate D；Multi-Document 不依赖本规格。

## 当前混合点

### Candidate 合同与纯原语

当前 `tm_sqlite_store.py` 同时定义：

- `CandidateProofIndexError`、`SQLiteStoreSchemaError`；
- `SQLiteCandidateRecord`、`SQLiteGramRow`、`SQLiteCandidateWritePlan`；
- recall/proof/block/dense phase DTO 与 opaque dense receipt；
- `CANDIDATE_INDEX_VERSION`、proof block version/size；
- n-gram、write-plan 与 dense result validator。

`tm_candidate_index.py` 直接导入这些名称及具体 store/query-view 类。首波会移动定义而不是复制定义；`tm_sqlite_store` 只 re-export 同一 class/function object。

历史名称中的 `SQLiteCandidate*` 本轮保留。改名会改变 `__name__`、repr、patch/import surface，无法再宣称严格行为等价；“中立”在本规格中指依赖方向与 port 行为，不通过顺手改名实现。

### Candidate 数据面

读路径集中在 recall、seed/proof/block/dense SQL；写路径集中在 write-plan validation、gram/FTS apply、block/max/digest、proof-index validation 与 streamed index build。当前函数同时可见 lease/transaction 与 SQL。提取时必须把 transaction 外壳留在 store，把只依赖 caller-owned connection 的 body 移入 projection。

## 合同设计

### 叶模块

新增 `tm_candidate_store_contracts.py`，只依赖 Python 标准库与必要的 frozen `tm_contracts` 版本常量。它不得导入 `sqlite3`、store、retrieval、Engine、Application 或 Qt。

叶模块拥有：

| 类别 | 合同 |
|---|---|
| 常量 | `CANDIDATE_INDEX_VERSION`、`CANDIDATE_PROOF_BLOCK_VERSION_V1`、`CANDIDATE_PROOF_BLOCK_SIZE` |
| 错误 | `SQLiteStoreSchemaError`、`CandidateProofIndexError` 的唯一 class authority |
| 写 DTO | `SQLiteCandidateRecord`、`SQLiteGramRow`、`SQLiteCandidateWritePlan` |
| 读 DTO | `SQLiteCandidateRecallSnapshot`、`SQLiteCandidateProofBlock`、`SQLiteCandidateProofRecord`、`SQLiteCandidateProofSnapshot` |
| dense DTO | `SQLiteCandidateProofDensePhase1/Phase2` 与不可伪造的 opaque receipt |
| 纯原语 | n-gram/frequency/write-plan builder、dense result/binding validator |
| port | `CandidatePostingPort`、`CandidateRecallPort`、`CandidateProofPort` 的 runtime-checkable behavior contract |

`SQLiteStoreSchemaError` 整体迁入叶模块，以保持 candidate algorithm 抛出/捕获的 exact class object；store re-export 同一对象，既有 consumer 不改变。它仍是 body-safe code 异常，不携 connection/path/query body。

### 读取 Port

`CandidatePostingPort` 只保留 fast/fallback seam 仍在使用的
`fts5_candidate_ids*` 与 `gram_candidate_overlaps` 行为，并要求
`candidate_port_scope == "STORE"`。`CandidateRecallPort` 最小行为：

- `resource_id: str`；
- `candidate_port_scope: "STORE" | "QUERY_VIEW"`；
- `candidate_recall_snapshot(...) -> SQLiteCandidateRecallSnapshot`；

`CandidateProofPort` 扩展 recall port：

- `candidate_proof_snapshot(...)`；
- `validate_candidate_proof_generation(...)`；
- `candidate_proof_block_records(...)`；
- `candidate_proof_dense_phase1(...)`；
- `candidate_proof_dense_phase2(...)`；
- view-owned generation/binding 验证行为。

Port 不暴露 connection、lease/token、coordinator 或 transaction。Algorithm 在调用前验证 built-in `resource_id` 与 callable 行为，在返回后继续 exact-type/nested copy；port 不能用 `Protocol` 通过就跳过 DTO 防伪。Store/query-view 自己仍先执行 lifetime/generation 检查。
`candidate_port_scope` 只区分 public store 与 captured query view 的既有调用形状，不携 generation、lease token 或 publication authority。Dense 结果先由叶合同纯 validator 验证 exact DTO/opaque receipt，再由 query view 验证 live generation/binding，二者都不得省略。

### Compatibility re-export

`tm_sqlite_store.py` 从叶模块显式导入并在 `__all__` 保留既有名称。以下不变量必须由测试证明：

```python
tm_sqlite_store.SQLiteCandidateRecord is (
    tm_candidate_store_contracts.SQLiteCandidateRecord
)
```

其他 DTO、错误、常量与纯原语同理。不得复制 wrapper class 或重新声明同名 dataclass。

## SQLite 数据面设计

### 模块与输入

新增 `tm_sqlite_candidate_projection.py`。它只依赖标准库 `sqlite3/hashlib/json`、叶合同和必要 frozen version 常量，不导入 store、candidate algorithm、retrieval、Engine/Application/Qt。

数据面函数接收：

- caller-owned `sqlite3.Connection`；
- 已由 store 入口 exact 验证并私有复制的 scalar/tuple/DTO；
- 必要的 generation/head/count/resource/store identity 值；
- 显式 `fts5_available` 与现有表/版本事实。

它返回叶 DTO、确定性 digest 或 `None`，但不返回 connection、transaction、lease、generation grant、receipt 或 capability。

### 读路径调用顺序

```text
Algorithm
  → Store/View port wrapper
      → lifetime + resource/generation check
      → open/reuse caller-owned connection
      → BEGIN read transaction
      → identity/head/count revalidation
      → projection recall/proof SQL + row decode
      → final generation/head/count revalidation
      → COMMIT / rollback
  ← exact leaf DTO
```

Store/query-view 继续决定 connection lifetime 与 BEGIN/COMMIT/ROLLBACK。Projection 不得在内部 `sqlite3.connect()`，不得调用 coordinator，也不得把 transaction completion 隐藏在 context manager 中。

### 写路径调用顺序

```text
Store append/streamed append
  → validate/copy drafts + complete candidate write plan
  → BEGIN existing canonical transaction
  → write canonical tm_record rows
  → projection apply gram/FTS/block/max/digest rows
  → projection validate proof index in same transaction
  → store update head/batch/publication facts
  → store COMMIT / rollback
```

Projection 的任一错误必须向既有 store wrapper 返回；wrapper 继续映射 `STORE.CANDIDATE_*` 并回滚整个 canonical transaction。数据面不得捕获 programmer fault 后伪装成输入错误，也不得自行 commit/rollback。

### Read/Write owner 切分

| 现有责任 | 新 owner | store 保留 |
|---|---|---|
| recall input SQL、FTS/fallback rows、folded-source decode | projection | lease、transaction、identity、error mapping |
| seed/proof block/maxima/dense SQL | projection | captured generation 与 lifetime |
| generation/head/count final check | store wrapper（可调用 projection 的纯查询 helper） | 最终授权判断 |
| gram/FTS/block/max/digest/index SQL | projection | canonical append transaction |
| streamed secondary-index suspend/restore/build SQL | projection | chunk transaction 与 completion |
| schema DDL/FTS5 probe | store | 全部保留，不在首波提取 |

## Fault Seam 与兼容策略

### Characterization inventory

在移动任何实现前冻结：

- production/tests 对 candidate 名称的 import 集；
- `tm_sqlite_store.<private>` patch target 集；
- callback/extension 调用顺序；
- read/write transaction 与 rollback 顺序；
- stable code、FTS5/fallback 双路径和 source-root evidence 清单。

### Late-bound wrapper

已被 patch 的 store private seam继续以同名模块级 wrapper 存在，并在 store call site 运行时解析：

```python
def _apply_candidate_write_plan(...):
    return candidate_projection.apply_candidate_write_plan(...)
```

Store 内部不得在 import 时把 wrapper 绑定到另一个局部别名，否则 `patch("tm_sqlite_store._apply_candidate_write_plan")` 会失效。新模块不反向调用 wrapper；wrapper 只单向委托唯一实现。

退役只发生在最终扫描证明 production/tests/evidence 均不再消费该 seam 时。无法证明的 wrapper保留，不把它记为未完成解耦；平行 SQL 实现才是 blocker。

## 失败语义

| 失败点 | 结果 |
|---|---|
| forged port / wrong resource identity | 首次 storage 调用前拒绝，无 scorer/transaction |
| forged DTO / nested value | hash、dedupe、SQL、callback 前拒绝 |
| recall/proof SQL 或 row decode | 同一 `STORE.CANDIDATE_*`，read rollback，无 partial snapshot |
| generation/head/count drift | `STORE.CANDIDATE_PROOF_STALE` 或既有 code，无成功 report |
| write plan/candidate SQL/index validation | canonical transaction 整体 rollback，旧 head/count/batch 保留 |
| programmer fault | 保持可观察，不折成 resource input failure |
| Gate C/D/evidence stale | feature NO-GO，Fuzzy 不由本规格自行授权 |

## 实施波次

### Wave 0：Characterization 与治理

- 冻结 import/patch/SQL/error/transaction/source-root inventory。
- 落地 ADR-017、R/D/T、review clustering 与 border。
- 不改 production runtime。

### Wave 1：Leaf contract 与 Algorithm port

- 移动 DTO/error/constants/pure validators 到叶模块。
- store 同一对象 re-export；candidate algorithm 改为中立 port。
- 暂不移动 SQL；先证明依赖方向和行为完全等价。

### Wave 2：SQLite read data plane

- 移动 recall/proof/block/dense SQL 与 row decode。
- store/query-view 保留 lifetime/transaction/generation wrapper。
- FTS5/fallback、sparse/dense、stale/fault 等价。

### Wave 3：SQLite write/projection data plane

- 移动 gram/FTS/block/max/digest/index build SQL。
- append/streamed transaction 仍由 store 完成。
- 保留 late-bound patch target并验证 rollback。

### Wave 4：Final roots、Evidence、Steering 与 GO

- 先只删除 closed scan 证明无 consumer 的 wrapper并完成最终 architecture cleanup，从而固定 production runtime；随后把 leaf/projection/store/index 同时写入 Gate C artifact/build roots、`BENCHMARK_IMPLEMENTATION_SOURCE_PATHS` 与 fault/acceptance closed source registry，并冻结全部 current-source roots。
- 在 registries 已冻结的同一 final fingerprint 上重放 store/retrieval 全量矩阵与 Gate C，再真实运行 Gate D。
- 最后只生成 fault/acceptance/release evidence并同步不属于 evidence roots 的治理文档；runtime 文件真实存在后同步 `structure.md`，归档 border，标 Feature GO。
- Gate C 开始后若 production、测试矩阵 registry 或任一 current-source root 再变化，所有后续证据一律 stale，必须从 Gate C 重新执行。

## 验证设计

### 合同与架构

- leaf module import allowlist；
- `tm_candidate_index` 禁止导入 store/projection concrete；
- projection 禁止导入 store/index/retrieval/Engine/Application/Qt；
- store re-export object identity；
- candidate SQL owner closed-world AST inventory。

### 行为等价

- 同一 input 的旧基线 fixture 与新 port：recall/proof/order/report/error exact equality；
- FTS5/fallback、short/long query、duplicate fold、dense/sparse、stale generation；
- append/streamed success 与 extension/SQL/chunk/commit faults；
- activation、reattestation、binding、snapshot、schema upgrade 与 migration 邻接回归。

### Evidence 与性能

- fault/acceptance/release matrix 重新生成并 strict 复读；
- Gate C roots 纳入 leaf/projection/store/index；
- Gate D implementation fingerprint 绑定最终 roots；
- 100,000 条 FTS5/fallback intended paths 真实运行，hard gates 不变；
- 任一路径失败即 release NO-GO。

## 风险与缓解

| 风险 | 缓解 |
|---|---|
| 只移动名字，algorithm 仍依赖 concrete store | architecture guard + port-only type annotation/runtime tests |
| transaction authority 随 SQL 移走 | projection 禁止 connect/commit/rollback/coordinator；fault order tests |
| wrapper 变成第二实现 | wrapper 只单行委托，SQL owner closed-world scan |
| 移动后 patch target失效 | Wave 0 inventory + runtime late-bound call tests |
| 为保旧 digest 不纳入新 roots | Gate C/D root closed-set 与 fingerprint mutation test |
| 维护线扩大为 TM 全面重写 | border 红线；schema/coordinator/contracts/parser/UI 均禁止修改 |

## Completion 条件

只有以下全部成立才可标记完成：

1. `tm_candidate_index.py` 不导入具体 store/query-view/projection；
2. 叶合同与 SQLite projection 各有唯一实现，store 只保留兼容入口/wrapper；
3. schema、generation、transaction、ordering、error 与磁盘行为等价；
4. 所有分波提交与 cluster review 闭合；
5. final source roots 上 fault/acceptance/release、Gate C 与真实 Gate D 全部通过；
6. ADR/Steering/border/tasks 与实际 tree 一致；
7. Multi-Document/Chunk/Sync/Parser/Qt 无越界变更。
