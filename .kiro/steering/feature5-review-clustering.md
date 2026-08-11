# Feature 5 评审集群与推理强度指南（采纳稿 v9）

本文件为 `tm-storage-retrieval-index` 剩余实施（Task 3.3–9.6）提供评审打包策略与 subagent 推理强度约束。受众是执行 Feature 5 的主 agent 及其 dispatch 的实施/复审 subagent。

逐小节评审会反复重建同一心智模型，返工循环由此产生。本指南把共享状态机、故障模型或流水线的子任务打包为评审集群，降低认知重建成本。Task 3.2 仍按旧流程完成最后一次单任务提交前复审；Cluster A 只在 3.2 验收提交落地后开始。

本采纳稿确立 A→L 集群拓扑，并根据 Task 3.2 的真实阻力为事务扩展、seal、跨文件发布、能力门、benchmark 证据和最终发布设置相匹配的复审强度。集群不是“少测几次”的理由，而是让评审发生在安全不变量真正闭合之后。

---

## 三级推理强度

仅设三级：`xhigh`、`high`、`medium`。不设 `low` 的理由：即使最机械的验收任务也需要核对 spec 条款、gate 新鲜度和测试退出码；`low` 与 `medium` 的成本差异远小于漏批风险。唯一可降为 `low` 的是 9.6 纯套件执行，但它仍以 `medium` 的代价换取 gate 一致性检查，不值得为此引入第四级。

| 级别 | 适用场景 | impl subagent | review subagent |
|------|----------|---------------|-----------------|
| `xhigh` | 崩溃恢复、多阶段 journal 幂等、原子多文件交换、对抗性 token 安全 | `xhigh` | `xhigh` |
| `high` | 并发 lease、状态机不变量、seal 安全、门控发布、双状态集成分支 | `high` | `high` |
| `medium` | 确定性算法、golden 向量、阶段守恒计数、覆盖映射、纯套件执行 | `medium` | `medium` |

复审强度不低于实施强度；崩溃恢复任务的复审比实施更难，因为复审者必须枚举实施者可能遗漏的故障路径。

### Subagent 模型与 effort 映射

表内三级强度是 Feature 5 的任务语义，不直接等同于不同 provider 的同名 effort。自 v2 起统一按下表 dispatch，并在每次集群证据中记录实际 agent type、provider/model 和 effort，禁止只写抽象强度：

| 集群强度 | 默认 agent 与 effort | 用途约束 |
|----------|----------------------|----------|
| `medium` | `v4_flash_worker` / `high` | 在 impl 列直接负责有明确边界与验收标准的实现，在 review 列负责机械核对和有界复审；任务输入必须自包含，并通过该 worker 当前适配 skill 交付。 |
| `high` | `v4_flash_worker` / `max` | 在 impl 列直接负责状态机、不变量和故障路径密集但仍可形成自包含 assignment 的实现，在 review 列执行相应复审；任务输入必须自包含，并通过该 worker 当前适配 skill 交付。 |
| `xhigh` | 原生 subagent / `xhigh` | 实施使用职责明确的 worker，复审使用 code-reviewer；允许读取当前任务上下文，但 assignment 仍须给出集群 base/tip、闭包和验证证据。 |

向外部 provider 发送私有源码、测试或规格前，必须已有用户对该 provider/data boundary 的明确授权。集群治理只依赖统一的 subagent 适配边界：自包含 assignment、明确读写权限与所有权、实际 agent/model/effort 记录、可靠的一次交付和原生结果回传。Hook、fork、transport 版本、provider API 或 runtime 调用方式均封装在对应适配 skill 内，不进入本集群地图；替换 agent/runtime 时只更新适配 skill，不改 Requirements、Design、Tasks 或集群安全不变量。

`impl` dispatch 必须由对应 subagent 在获分配的文件边界内直接实现并运行定点验证；只返回实施分析或蓝图不算完成 impl dispatch。主 agent 保留变更核验、跨任务整合、显式暂存与提交责任。

---

## 评审集群地图

以下 12 个功能集群与 2 个行为保持型结构门覆盖 Task 3.3–9.6 全部剩余子任务及 5.R1/5.R2/5.R3。打包原则：复审者需要同一心智模型才能发现缺陷的子任务归为一组；结构门只证明已批准行为的移动等价性。

| 集群 | 子任务 | 共享心智模型 | impl | review | 评审轮次 | 实施体量 |
|------|--------|-------------|------|--------|---------|----------|
| A — Store lifecycle | 3.3 + 3.4 | generation lease/drain ↔ canonical/source-binding 状态机 | `high` | `xhigh` | 1 | 小 |
| B — Candidate index | 4.1 + 4.2 + 4.3 | FTS5 trigram → gram fallback → merge/budget/metadata 召回流水线；与 record/index 同事务 | `medium` | `high` | 1 | 小 |
| C — Stage build + seal | 5.1 + 5.2 + 5.3 + 5.4 | preflight → mutable stage → seal/integrity → Gate B 证据与 opaque artifact | `high` | `xhigh` | 1 | 中 |
| D — Activation + recovery | 5.5 + 5.6 + 5.7 + 5.8 + 5.9 + 5.R1 | journal 状态机 `PREPARED→DB_REPLACED→MANIFEST_PUBLISHED→GENERATION_PUBLISHED` + 成套回滚 + 行为保持型模块提取 | 5.5/5.7 `xhigh`；5.6/5.8/5.9/5.R1 `high` | `xhigh` | 1 | 大 |
| E — Divergence + upgrade | 5.10 + 5.11 | 复用 seal/activate 的显式消歧与 schema copy-switch | `high` | `high` | 1 | 小 |
| E-R — Upgrade boundary | 5.R2 | 已批准 schema-upgrade 故障模型的等价提取 + 依赖方向守卫 | `high` | `high` | 1 | 中 |
| F — Export + snapshot | 5.12 + 5.13 + 5.14 | 任意路径导出与配置快照发布协议 + 崩溃恢复矩阵 | `high` | `xhigh` | 1 | 中 |
| F-R — Snapshot artifact boundary | 5.R3 | 已批准 export/refresh/recovery 命名空间故障模型的纯等价提取 + mutation-proof 依赖守卫 | `high` | `high` | 1 | 大 |
| G — Facade integration | 6.1 + 6.2 + 6.3 | legacy/CURRENT/HISTORY/DIVERGED/unhealthy 冷启动权威矩阵 + 兼容回归断言 | `medium` | `high` | 1 | 中 |
| H — Retrieval pipeline | 7.1 + 7.2 + 7.3 | exact/context 分类 → fuzzy 评分 → 多资源聚合排序/部分失败 | `high` | `high` | 1 | 中 |
| I — Capability gate C | 7.4 + 7.5 | 独立 CONTEXT/FUZZY 可用性门 + 证据聚合且不撤销 exact/save | `medium` | `high` | 1 | 小 |
| J — Benchmark subsystem | 8.1 + 8.2 + 8.3 + 8.4 + 8.5 | 确定性语料 → 延迟/RSS 运行器 → oracle recall → 双路径硬门 | `medium` | `high` | 1 | 大 |
| K — Fault injection | 9.1 + 9.2 | 全量迁移/激活/快照故障矩阵，验证 D+F+E 的负空间 | `high` | `xhigh` | 1 | 中 |
| L — Evidence + release | 9.3 + 9.4 + 9.5 + 9.6 | 跨域能力矩阵 + 兼容回归 + 86 条覆盖映射 + 完整发布 | `medium` | `xhigh` | 1 | 中 |

复审数量从逐小节 38 次降至 12 个功能集群复审加 2 个等价性结构门。

---

## 集群认知成本简述

**A — Store lifecycle：** lease/drain 是 canonical 状态机的并发基座。3.4 的 source-binding observation 与 revision 变化必须经同一 generation 视图，不得绕过 lease。复审同时检查有界等待、跨 generation 连接隔离、last-known-good authority 和禁止双向隐式覆盖。

**B — Candidate index：** 完整召回流水线，正确性只在三者完成后可判断。Task 3.2 已证明 candidate 扩展同时触及 record/index transaction ownership；因此其爆炸半径不只包含 fuzzy 质量，还包含 canonical append 原子性。复审必须重验“nested value 先验证、后 hash/dedupe/SQL”的拒绝顺序。

**C — Stage build + seal：** preflight 正确才有 stage 正确，stage 完整才有 seal 意义，Gate B 聚合证据。seal 对抗性要求（先闭合全部 indexes/count/parity，再 close/fsync；artifact mutation、registry/ref、resource/binding mismatch 均拒绝）要求 `xhigh` 复审。

**D — Activation + recovery：** Feature 5 最高风险区域。journal 四阶段每个恢复分支必须产生一个完整 generation。token 单次消费、nonce 防重放、成套回滚（DB + manifest 同时恢复）是对抗向量。v4 将实施强度按任务阻力临时细分：5.5 负责 capability、lease drain 与备份 authority，5.7 定义正常激活和两条恢复路径共同依赖的发布线性化点，二者保持 `xhigh`；5.6、5.8、5.9 在这两条边界上实现有明确 phase/验收矩阵的 journal 与恢复分支，降为 `high`。v5 在 5.9 闭合恢复矩阵后加入 5.R1：只把 journal/terminal codec、durable file protocol 与逐 phase recovery/rollback 提取到设计指定模块，不改变行为。它仍共享 D 的全部故障模型，因此不另建评审簇；Cluster D reviewer 只在 5.R1 完成后读取最终模块形态一次，并同时核对 characterization/failure matrix 与依赖方向守卫。该调整不改变单次复审时机或 `xhigh` 复审强度。
**E — Divergence + upgrade：** 复用 D 的 seal/activate 流水线。新成本仅在 divergence 清除条件和 schema upgrade 复制切换。复审者已在 D 加载激活模型，成本递减。

**E-R — Upgrade boundary：** Cluster E 已先以原始模块形态闭合功能和故障矩阵；5.R2 只在此稳定基线上提取 schema-upgrade copy/artifact 数据面。实施和复审均使用 `high`，因为移动横跨 `tm_sqlite_store.py` 和 `tm_migration.py` 的 private seam，但不重新设计 coordinator 状态机。验收必须重用已批准 Cluster E 断言，检查新模块不反向导入两个 owner，并证明 patch/fault-injection seam、错误码、cleanup 顺序和磁盘效果不变。异常分支简化不属于该门，避免同时改变布局与行为而放大回归风险。

**F — Export + snapshot：** 5.12 不改变活动 binding，5.13/5.14 才改变配置快照；两者共享 temporary/fsync/replace/manifest 协议但发布后果不同。复审必须分别证明任意路径损坏不污染 authority，以及 issued receipt 恢复只能完成、取消或进入 divergence。

**F-R — Snapshot artifact boundary：** Cluster F 先以原模块形态闭合发布/恢复状态机和命名空间故障矩阵；5.R3 才把 deterministic artifact family、root→parent no-follow dirfd 绑定、strict single-link identity/digest proof、exclusive temp/recovery copy、replace/cleanup 原语与 durable handoff 值编解码移入独立模块。`tm_migration.py` 仍拥有对外导出/刷新编排与成败 outcome，`tm_snapshot_recovery.py` 仍拥有 receipt 分类、reconciliation 和 terminal replay 状态机，`tm_sqlite_store.py` 仍独占 ledger/binding/transaction/coordinator 权威。实施和复审均使用 `high`；必须保留全部 error code、fault seam、mutation 顺序、durable handoff 生命周期和磁盘效果。异常分支简化属于正交治理，禁止混入该门。

**G — Facade integration：** 导入接缝切换、exact 查询切换和三态兼容验证。6.3 不是新代码，是回归断言。复审必须以进程级重开为边界，覆盖 never-activated、cancelled-first、CURRENT、合法写后 HISTORY、SOURCE_DIVERGED 与 canonical artifact 不可证的 fail-stop；只有前两种可以使用 legacy JSONL。普通 save/import 使 snapshot 落后不是 activation recovery 失败，而是 monitor 在 canonical generation 恢复后派生的 `VERIFIED_HISTORY`。

**H — Retrieval pipeline：** exact/context 分类 + fuzzy 评分 + 全局排序是一条流水线。7.3 的部分失败和 global limit 语义只能对照 7.1+7.2 验证。

**I — Capability gate C：** CONTEXT 与 FUZZY 独立可用性门 + Gate C 证据聚合。任一子门失败只能关闭自身，不得撤销 canonical exact/save 或另一个已验证子门，因此 review 提升为 `high`。

**J — Benchmark subsystem：** 自包含测量流水线。"数字是否满足硬门"只是最后一步；复审还要排除有利样本选择、digest/环境/统计口径漂移、fast-path 成功掩盖 fallback 失败。oracle recall=100% 是正确性硬约束。

**K — Fault injection：** 测试 D + F + E 的负空间。共享同一方法论和"prior 资产必须存活"不变量。

**L — Evidence + release：** 跨域能力矩阵、兼容回归、86 条覆盖映射和完整发布验证。它是 Feature GO 的最后裁决，不得把任务勾选或测试数量当作 coverage；因此 final review 使用 `xhigh`。

---

## Task 3.2 复盘：为什么单任务复审仍然卡住

Task 3.2 的 raw exact、variant history 和 SQLite transaction 主路径较早就通过机械测试；真正反复失败的是一个跨层不变量：**任何 caller-owned 值必须先通过 exact-type 与 nested validation，并重建为实现私有的不可变快照，之后才能 hash、比较、去重、fold、执行 callback、进入 transaction 或被长期 handle 保存。**

旧循环的返工链依次暴露了 connection-bearing callback、live-transaction frame 可达性、scalar subclass 延迟执行、动态 `str/int` 改值、nested row 在验证前 `set()` 去重，以及 store handle 保存 caller-owned frozen stage 后被重定向到另一资源。它们不是彼此独立的 bug，而是同一个“拒绝顺序和值闭包没有先整体设计”的根因。每次只修 reviewer 当轮给出的具体入口，会把同一不变量的下一个变体留到下一轮。

采纳经验：

1. 子任务实现提交前先写出本任务的不变量闭包清单；机械测试只能证明已列出的路径，不能替代闭包盘点。
2. 对含 callback、opaque token、frozen nested contract、transaction 或 file replace 的代码，验证顺序本身就是设计：`exact type/identity → nested validation → private snapshot/seal → dedupe/hash/compare → side effect`。`frozen=True` 不是信任边界；长期对象不得继续回读调用方持有的 contract 引用。
3. 小步提交与对抗复审解耦：子任务稳定后提交，集群 reviewer 一次读取 `cluster-base..cluster-tip` 的完整补丁和状态机，不再只看最后一个小 diff。
4. reviewer 若发现问题，修正形成独立提交；定点复验复用已经加载的集群心智模型，不重新拆回逐子任务全面复审。
5. 独立缺陷若在一个子任务内已闭合且不与其他成员共享状态，可以临时单独复审；Task 3.2 即此例，也是最后一次提交前单任务复审。

---

## 动态调整规则

本地图是可演进的治理基线，而非刚性约束。主 agent 在推进中可基于以下信号调整：

- 实施中发现两个集群共享更多状态或不变量 → 合并。
- 阻力集中在某个子任务的独立缺陷（与其他成员无状态耦合）→ 对该子任务单独复审一次，不影响其余打包。
- 前序集群复审暴露了后续集群的共享缺陷 → 提升后续集群的复审强度。

### 实施体量与复审时机的关系

"实施体量"列表达的是实施工作的大致规模，帮助主 agent 预估 token 和时间预算。它不规定实施者怎么切自己的工作节奏——实施者按自然进度工作，中间随时跑机械测试确认不 break build。

复审时机的唯一判断标准是：集群内部的安全不变量是否已经闭合。如果不变量跨多个子任务才能闭合（典型如 Cluster D 的 journal 恢复矩阵），就必须等全部子任务实现完毕后才做对抗性复审——提前复审意味着复审者只看到半个状态机，要么误批后续会推翻的设计，要么被迫重建完整推理，这正是返工循环的来源。如果某个子任务的安全不变量在自身范围内就闭合了，那它本来就不应该和同集群其他成员打包。

调整时更新本文件版本行，保持可追溯。

---

## 实施、提交与集群复审节奏

### 集群实施准备门

- 首个实现 assignment 下发前，主 agent 先形成 provider-independent implementation invariant capsule：固定已批准的 Requirements/Design/Task 条款、cluster base、消费的前序权威状态、只能显式清除的 latch、fail-stop 负空间、当前切片的精确 owner/exclusion、验证合同与停止条件。后续切片只引用并收紧该 capsule，不能从邻近代码、旧会话摘要或实施者记忆重新猜测任务权威。
- 含 file replace/rename/delete 或 journal recovery 的集群，mutation-proof ledger 是实施输入而不是复审后补文档；实现发现新 mutation seam 时必须先补齐该 seam 的 authority snapshot、parent/path identity、pre-mutation 复证、线性化变更、post-mutation durability/proof 与 crash replay，再继续扩展代码。
- assignment 大小按“一个连贯实现切片内需要重建多少独立状态机/故障模型和 owner 权威”裁剪，不以文件行数裁剪。若同一 assignment 需要反复重建多个独立模型，先做只读侦察，再按 owner seam 拆成更窄的实现切片，集群级对抗复审仍保持一次。
- 实施返回、进度叙述与磁盘状态冲突时，以目标 Git root、branch/base、owned-path diff 和主 agent 新鲜验证为整合权威；身份或变更声明不闭合的 contribution 先拒绝整合并审计适配边界，不能仅凭“后来出现了预期代码”或一条异常消息判定 transport 串线。

### 子任务实现提交

- 子任务或紧密任务组达到 task completion definition 后，运行 task-focused tests、相关 static check 和 `git diff --check`；确认不 break 当前 build 后即可勾选并提交，不 dispatch 对抗性 reviewer。
- 每次显式暂存 task 代码、测试、证据和 `tasks.md`，用 `git diff --cached --name-status` 核对；禁止整目录或通配暂存 `.kiro` / `.opencode`。
- 预存在 worktree 的其他任务 diff、用户文件或只读相邻 Spec 不得混入提交。

### 集群闭合门

只有以下条件同时满足才 dispatch 一次 cluster reviewer：

1. 集群内全部子任务已有小步提交且 checkbox 为 `[x]`；
2. 集群共享状态机、故障模型或流水线的正向与失败分支已经实现，不以“后续任务会补”作为当前不变量；
3. task-focused mechanical checks 全绿，且不存在未分类的新失败；
4. 主 agent 固定 cluster base commit、tip commit、累计 diff、共享不变量清单、故障矩阵和已知外部基线，并与实施前 capsule 逐项核对；
5. 主 agent 把实施前 capsule 更新为 downstream invariant capsule：记录最终权威转移、只能显式清除的 latch 和 fail-stop 负空间，并压缩为后续集群 assignment 与 fresh-process 验收用例；不得只依赖长篇 Implementation Notes 或实施者记忆；
6. 含 file replace/rename/delete 或 journal recovery 的集群完成实施前 mutation-proof ledger 的最终对账；任一 mutation seam 缺少 authority snapshot、parent/path identity、最后一次 pre-mutation 复证、线性化变更、post-mutation durability/proof 或 crash replay 结果，都表明不变量尚未闭合；
7. reviewer 使用表中 `review` 强度，对累计补丁和最终状态做一次对抗性复审。

复审发现问题后只实施 concrete findings，形成独立修正提交并复用同一 reviewer 心智模型做定点复验。复审通过后，主 agent 才运行一次 fresh full suite 并记录集群验证证据；不在每个子任务重复 full suite + adversarial review。

### 已知外部失败基线

- 根目录 `test_malformed_artifact.py` 的 1 个失败属于 Feature 3 comparative-report artifact 遗留，不归 `tm-storage-retrieval-index` 所有，也不在 feature5 分支修复。
- 它必须在验证报告中单列为 pre-existing external baseline：相同失败不阻断 Feature 5 子任务提交，但不得被写成“全仓零失败”，也不得掩盖任何新增失败、错误签名变化或 Feature 5 测试失败。
- Feature 5 的 canonical Python suite 仍以 `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v` 为主；集群/发布验证另行列出根目录遗留脚本结果。

---

| 版本 | 日期 | 变更 |
|------|------|------|
| v9 | 2026-08-12 | 将 invariant capsule 与 mutation-proof ledger 从集群复审前移到首次实现 dispatch；按独立状态机/owner 权威而非文件行数裁剪 assignment，并以目标 worktree 的 Git 身份、磁盘 diff 与新鲜验证裁决返回叙述冲突。 |
| v8 | 2026-08-11 | 将 Cluster G 从简化双态分支收紧为 legacy/CURRENT/HISTORY/DIVERGED/unhealthy 冷启动权威矩阵；在集群闭合门加入 provider-independent downstream invariant capsule，防止前序已批准语义在后续实施中丢失。 |
| v7 | 2026-08-11 | Cluster F 闭合后增加 5.R3/F-R 纯等价快照 artifact 边界提取；将 mutation-proof ledger 加入所有文件发布/恢复集群的闭合门，异常分支简化保持正交。 |
| v6 | 2026-08-10 | 在 Cluster E 闭合后、Cluster F 开始前增加 5.R2/E-R 等价性结构门；提取 schema-upgrade copy/artifact 数据面，保留 coordinator/migration 权威与异常分支语义。 |
| v5 | 2026-08-09 | 在 5.9 后加入行为保持型结构门 5.R1，将 activation/recovery 提取纳入 Cluster D 的同一次最终复审；保持既有功能编号、故障模型和复审强度。 |
| v4 | 2026-08-09 | 按实际阻力细分 Cluster D 实施强度：5.5/5.7 保持 xhigh，5.6/5.8/5.9 调整为 high；不改变共享不变量与复审时机。 |
| v3 | 2026-08-09 | 删除与后续集群无关的一次性历史例外；以 provider adapter skill 隔离 Hook、fork、transport 与 runtime 细节；明确 impl subagent 必须直接实施而非只交付分析。 |
| v2 | 2026-08-09 | 固化跨 provider 调度语义：`medium→v4/high`、`high→v4/max`、`xhigh→原生/xhigh`；加入 plaintext Hook、私有源码授权和实际 dispatch 记录要求。 |
| v1 | 2026-08-01 | 确立 A→L 集群边界与三级推理强度；加入 Task 3.2 exact-type、私有快照和值闭包复盘，以及集群闭合门、提交/审批解耦和 Feature 3 外部失败基线。 |
