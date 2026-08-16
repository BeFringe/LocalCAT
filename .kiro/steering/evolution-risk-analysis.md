# Evolution Risk Analysis v1.2

> LocalCAT 治理系统演进风险分析。独立于基线文档，提供详细的风险上下文和漂移模式说明。
> 风险优先级表见 governance-baseline.md 第 4 节。

---

## 1. 风险总览

当前阶段：治理系统 v2 接入期。ADR 与 border 生命周期已经建立，cc-sdd 已加入 scope lineage、ADR/Steering disposition 与 Feature GO 闭合点。

下文 R1～R7 保留 2026-05 初始风险的形成背景。当前残余风险不再是“没有文件”，而是新决策漏提、草案被误当授权、同步处置被跳过，以及阶段门未真实执行。

### 2026-08-16 校准

- R1：ADR-001～006 已采纳，ADR-007～011 为待审草案；严重度由“机制缺位”转为“漏提/未决”中风险。
- R2：Steering 同步机制已接入 Requirements、Design、Tasks 与 Feature GO；残余风险是执行遗漏。
- R3：border.md 的创建、执行、归档和 ADR 提升通道已定义；残余风险是功能归档时漏做。
- R5：项目规则已由 cc-sdd Skills 显式加载；后续监控真实入口覆盖率，不再另造治理立法 Skill。

---

## 2. 2026-05 高风险形成背景

### R1: ADR 缺位（历史基线）

**严重度：** 🔴 高
**当前状态：** 活跃
**触发条件：** 不可逆决策散落在 tech.md 和 spec 中，无治理保护

**具体表现：**
tech.md 中记录了多个架构决策，但它们是作为"技术栈说明"存在，而非正式的 ADR：
- Trie 用于 Glossary（存储策略决策）
- JSONL append-only TM（存储格式决策）
- Stateless LogicController（架构模式决策）
- TM-priority strategy（业务逻辑决策）
- 条件性 openpyxl import（依赖管理决策）

parser-subsystem design.md 中也包含 ADR 级别的架构决策：
- Parser → Engine 单向依赖（子系统边界决策）
- ParserRegistry 全局单例定位（组件职责决策）
- BaseParser 作为唯一抽象接口（扩展策略决策）

**后果：**
- 后续功能可能无意中引入违反这些决策的实现（如 Parser 直接依赖 TM Engine 内部结构）
- Agent 或人类开发者无法区分"临时选择"和"不可逆决策"
- tech.md 和 spec 归档后，这些决策失去可见性

**缓解策略：**
1. 从 tech.md 提取已有架构决策，建立初始 ADR
2. 从 parser-subsystem spec 中识别应提升为 ADR 的内容
3. 为未来决策建立 ADR 创建触发条件

**风险消除条件：** 所有散落的架构决策已提取为正式 ADR，且 ADR 创建流程已建立。

---

### R2: Steering 文档静止

**严重度：** 🔴 高
**当前状态：** 活跃
**触发条件：** 架构已演化，Steering 缺乏同步更新机制

**具体表现：**
- structure.md 规划了 L2A（Core Matching）/ L2B（Parser/Import-Export）的拆分，但这是"规划"而非"已实施"
- parser-subsystem spec 设计了 Layer 2B 的完整组件架构，部分内容可能与 structure.md 的描述产生矛盾
- tech.md 记录的是 Phase 1-3 的技术状态，Phase 4+ 的技术变化尚未反映

**后果：**
- Agent 基于过时的 Steering 做决策（如不知道 L2B 已有正式的组件架构）
- 新功能的 cc-sdd spec 可能与实际架构方向矛盾
- "两个真相源"：Steering 说的一套，实际代码做的一套

**缓解策略：**
1. 在每次 Spec design review 时，检查 Steering 是否需要同步更新
2. architect-decision 增加触发条件：当 Spec 设计与 Steering 描述矛盾时，标记为"Steering 同步需求"
3. 建立 Steering 更新的显式流程（非 Agent 自主修改，而是标记 + 人工确认）

**风险消除条件：** Steering 与当前架构保持一致，且有机制保证未来同步。

---

### R3: border.md 归属模糊

**严重度：** 🔴 高
**当前状态：** 活跃
**触发条件：** 需求级红线生命周期未治理，存在约束丢失风险

**具体表现：**
architect-decision 定义 border.md 为"per-requirement 红线"，但以下问题未定义：
- **谁创建？** — Agent 在提取 Session Requirements 时自动创建？还是需要人工确认？
- **何时创建？** — 每次新需求开始时？还是每次会话开始时？
- **归档到哪？** — 需求完成后 border.md 是删除、归档到 Spec 目录、还是提升为 ADR？
- **版本管理？** — 同一个需求的 border.md 在多次会话间是否一致？

**活例证：** parser-subsystem-extraction 产生了 33 个 correctness properties。这些是需求级红线还是架构级约束？如果是需求级，功能完成后它们何去何从？如果是架构级，它们应该被提升为 ADR。

**后果：**
- 需求级红线随功能完成而丢失（约束悬空）
- 或留在已归档的 Spec 中，失去治理保护
- 不同会话对同一需求的 border.md 提取不一致

**缓解策略：**
1. 定义 border.md 的生命周期：创建 → 确认 → 执行 → 归档
2. 归档时检查：哪些约束应提升为 ADR，哪些随 Spec 归档
3. 将 border.md 生命周期纳入治理 Taxonomy

**风险消除条件：** border.md 的创建者、创建时机、归档流程、版本管理均已定义并记录。

---

## 3. 中风险

### R4: Spec-Steering 边界侵蚀

**严重度：** 🟡 中
**触发条件：** Spec 承载了超出 feature 生命周期的架构决策

**具体表现：**
parser-subsystem design.md（903 行）包含：
- 5 个组件的全局定位和职责定义
- 3 个跨层数据模型（SourceUnit, TMEntry, ParseError）
- 4 个明确的扩展点

这些内容描述的是 Layer 2B 的长期架构，而非 parser-subsystem 功能的实现细节。功能完成后，这些架构决策仍需长期有效，但它们"住在"一个功能级的 Spec 中。

**漂移模式：** 层级混淆（Layer Confusion）——Spec 承载了 ADR 或 Steering 级内容。

**缓解策略：**
- design review 阶段，architect-decision 识别并标记 Spec 中的全局决策
- 全局决策提升为 ADR 候选

---

### R5: Skill 触发覆盖率不足

**严重度：** 🟡 中
**触发条件：** 治理机制未覆盖全生命周期

**具体表现：**
architect-decision 的触发条件聚焦于"实现阶段"：
- Agent 提议不实现
- 分步验证
- 上下文压缩

但以下阶段未覆盖：
- 新功能设计阶段（设计是否违反 Steering？）
- ADR 提取（何时应创建新 ADR？）
- Steering 更新决策（何时 Steering 应同步？）

**漂移模式：** 治理盲区导致静默覆盖。

**缓解策略：**
- 扩展 architect-decision 触发条件，或
- 引入新的治理 Skill（如 adr-extractor、steering-sync-checker）

---

## 4. 低风险

### R6: Steering 演进失控

**严重度：** 🟢 低
**触发条件：** 当前主 Agent 集中治理，缺乏触发条件

**当前无风险原因：** 治理由单一 Agent 在单一会话链中执行，不存在多人/多会话冲突。当治理参与方增加时需重新评估。

### R7: 灰线判定主观性

**严重度：** 🟢 低
**触发条件：** 灰线判定依赖 Agent 理解

**具体表现：** "功能等价"和"合理实现差异"的判定标准是定性的。不同 Agent 实例可能做出不同判定。

**缓解策略：** 建立灰线先例库，为常见场景提供判定参考。这是 P2 优先级，不阻塞当前阶段。

---

## 5. 漂移模式详细说明

### 5.1 静默覆盖（Silent Override）

**定义：** 无显式决策记录的架构变更。

**典型场景：**
- ADR 缺位时，新功能无意中违反了已有架构决策
- "暂不实现"成为永久的隐式降级，但无决策记录

**检测方法：** architect-decision 的 Decision Gating + ADR 追溯检查。

### 5.2 层级混淆（Layer Confusion）

**定义：** 治理工件承载了不属于其层级的内容。

**典型场景：**
- Spec 包含 ADR 级架构决策
- Skill 尝试定义治理内容而非执行治理
- Steering 写入功能级实现细节

**检测方法：** 定期审查治理工件的内容边界。

### 5.3 隐式降级（Implicit Downgrade）

**定义：** 以"暂不实现"为由降级设计要求，不产生决策记录。

**典型场景：**
- Agent 认为某个功能"锦上添花"而跳过，但该功能实际影响验证目标
- 多次"暂不实现"累积为显著的能力缺失

**检测方法：** architect-decision 的 Verification Anchoring + Decision Gating。

### 5.4 反向依赖（Reverse Dependency）

**定义：** 治理工件的修改依赖实现代码验证。

**典型场景：**
- Steering 更新需要先实现代码来验证正确性
- ADR 变更需要先看代码才能确认影响范围

**检测方法：** 治理工件变更前检查是否已耦合实现。

### 5.5 跨会话解释漂移（Cross-Session Interpretation Drift）

**定义：** 不同会话对同一 Steering 条款产生不同解释。

**典型场景：**
- 会话 A 理解"Layer-first flat-file"为"永远不分包"
- 会话 B 理解为"当前不分包，但 Layer 2B 可能需要"
- 两个理解都"合理"，但导致不同决策方向

**检测方法：** 无现有机制。需要解释一致性校验或术语表强制引用。

---

## 6. 风险监控建议

| 风险 | 监控信号 | 检查频率 |
|------|----------|----------|
| R1 ADR 缺位 | 新 Spec 包含架构决策但未创建 ADR | 每次 Spec review |
| R2 Steering 静止 | Spec 设计与 Steering 描述矛盾 | 每次 Spec review |
| R3 border.md 归属 | 功能完成后红线约束无归属 | 每次功能归档 |
| R4 边界侵蚀 | Spec 内容超出功能级范围 | 每次 Spec review |
| R5 Skill 覆盖率 | 治理事件未被 Skill 拦截 | 每次会话结束时回顾 |
| R6 演进失控 | Steering 单次变更幅度过大 | 每次 Steering 变更 |
| R7 灰线主观性 | 同类场景产生不同判定 | 积累案例后分析 |

---

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-05-13 | 初始版本 |
| v1.1 | 2026-05-13 | 校准修正：R1 拆分为"ADR 缺位"和"Steering 文档静止"两个独立风险；R3 border.md 归属模糊从 🟠 中低恢复为 🔴 高；增加漂移模式详细说明和监控建议 |
