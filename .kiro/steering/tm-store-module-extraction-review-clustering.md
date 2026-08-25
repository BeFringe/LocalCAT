# TM Store Candidate 模块提取复审分簇

> 本文件只定义 `tm-store-module-extraction` 的实施/复审节奏，不扩张功能范围。每个 Wave 形成一次阶段性提交；reviewer 审查该 Cluster 的累计 diff 与最终状态，而不是逐函数重复重建上下文。

## 共同不变量胶囊

- schema v2、持久对象、generation、transaction、stable code、candidate ordering、proof-query-v3、scorer/budget/threshold/top-k 均不变。
- `ResourceStoreCoordinator`、`SQLiteTMStore`、`SQLiteTMQueryView` 分别保留 coordinator、transaction/public 入口、captured-generation lifetime 权威。
- 叶合同不导入 store/retrieval/Engine/Application/Qt；projection 不导入 store/index/retrieval/Engine/Application/Qt。
- candidate algorithm 不导入 concrete store/query-view/projection。
- compatibility wrapper 只委托唯一实现；不得存在平行 SQL/validator。
- 新 roots 必须让旧 Gate C/D/current-source evidence 失效，最终必须真实重跑。

## Cluster 0：治理与 Characterization

- **Tasks**：1.1–1.4
- **实施提交**：`docs(tm-store): 冻结 candidate 模块提取治理`
- **共享风险**：迁移表遗漏、fault patch target遗漏、把 Gate D 100k误作通用容量、把行数清理扩成 authority 迁移。
- **通过条件**：R/D/T/ADR/border一致；import/patch/SQL/transaction/evidence inventory 可执行；production 零变化。

## Cluster 1：叶合同与 Port

- **Tasks**：2.1–2.4
- **实施提交**：`refactor(tm): 建立 candidate storage 中立 port`
- **共享风险**：class object漂移、compat subclass、Protocol 伪造、algorithm仍通过 concrete type或 coordinator属性取权威。
- **对抗重点**：object identity、forged nested DTO、resource mismatch、port property/callable fault、store import closed-world。
- **通过条件**：algorithm只依赖叶合同；SQL未移动；所有基线行为 exact equality。

## Cluster 2：SQLite 读数据面

- **Tasks**：3.1–3.4
- **实施提交**：`refactor(tm): 提取 SQLite candidate 读数据面`
- **共享风险**：projection偷偷打开 connection/transaction、generation final check迁权、FTS5/fallback顺序漂移、dense receipt失去 binding。
- **对抗重点**：read transaction call order、stale generation、sparse/dense/frontier、body-safe errors、late-bound patch target。
- **通过条件**：read SQL唯一 owner；store/view仍拥有 lifetime与transaction；双路径等价。

## Cluster 3：SQLite 写/Projection 数据面

- **Tasks**：4.1–4.4
- **实施提交**：`refactor(tm): 提取 SQLite candidate 写数据面`
- **共享风险**：candidate与canonical记录不再同事务、streamed chunk部分提交、proof index/digest时序漂移、wrapper绑定过早导致fault seam失效。
- **对抗重点**：extension/plan/SQL/summary/chunk/commit fault、old head/count/batch、activation/schema/snapshot/migration邻接。
- **通过条件**：写 SQL唯一 owner；transaction completion仍在store；所有故障LKG等价。

## Cluster 4：Current-source Evidence 与 GO

- **Tasks**：5.1–5.5
- **证据提交**：`test(tm): 重签 candidate 提取 current-source 证据`
- **共享风险**：旧root纯重签、只跑FTS5、fingerprint与evidence不在同一commit、wrapper伪装implementation root未变、release提前GO。
- **对抗重点**：closed root set、Gate C fresh、100k FTS5/fallback、fault/acceptance/release strict复读、最终治理门。
- **通过条件**：全部证据绑定同一final tree；任一路径失败均NO-GO；Steering/border/tasks与实际一致。

## 提交与复审规则

1. 每个 Cluster 只有一个阶段性实现提交；remediation 使用紧随其后的窄修复提交，不重写已审计的前序 Cluster。
2. 暂存必须显式列出 owned paths；`.DS_Store`、用户样本、相邻 Spec 与工作文件不得进入提交。
3. Cluster review 前记录 base/tip、累计 diff、共享不变量、fault matrix 与 fresh focused results。
4. reviewer 发现 blocker 后只修 concrete finding，并复用同一 reviewer 做定点复验。
5. full Gate C/D 只在 final runtime roots 稳定后运行一次；前序 Wave 不伪造临时资格。

| 版本 | 日期 | 变更 |
|---|---|---|
| v1 | 2026-08-21 | 冻结治理、leaf port、read、write、evidence 五簇及每 Wave 一次阶段提交。 |
