# Steering 同步机制

> 确保 Steering 文档（product.md / structure.md / tech.md）与项目实际架构保持一致。
> 解决治理基线中识别的 Steering 文档静止和 border.md 归属模糊问题。详见 `evolution-risk-analysis.md`。

---

## 1. 触发事件清单

| 事件 | 触发时机 | 影响范围 |
|------|----------|----------|
| Design 预期同步 | design.md 通过评审时，仅声明可能受影响的 Steering | structure.md / tech.md |
| ADR 获批 | 新 ADR 经人工批准后 | tech.md（去重） |
| Feature GO | 按实际 tree/runtime 一次闭合 Design 声明的同步面 | product.md / structure.md / tech.md |
| border.md 归档 | 功能完成后 | steering/borders/ |
| 架构重构 | 改变层级结构或依赖方向 | product / structure / tech |
| Steering 引用冲突 | 实现中发现 Steering 与实际不符 | 涉及的具体文件 |

## 1.1 cc-sdd 阶段接入

`.kiro/settings/rules/governance.md` 是本机制在 cc-sdd 中的执行入口：

1. **Requirements**：范围修订或相邻 Spec 依赖必须记录 `Scope Lineage`，防止曾明确排除的能力被代码静默纳入。
2. **Design**：每份设计必须记录 `Governance Impact`，明确适用 Steering/ADR、候选或取代关系、scope amendment 与同步结论。
3. **Tasks**：只有存在治理影响时才生成显式闭合任务；无影响时保留设计中的“无需同步”理由。
4. **Implementation**：task/cluster 准备时只做无痕语义门检查；未跨门槛时继续且不追加同步记录。跨过门槛但获批 Design 已精确映射到 adopted ADR、且变化未越界时按既有授权继续；否则停止并回到 ADR candidate 与人工审批。
5. **Feature GO**：以最终 diff/tree/runtime 对照获批 Requirements/Design/Tasks 与五类语义门槛，先阻断实施期漏报或未授权的实际 delta，再一次闭合 Design 声明的 Steering disposition；仍影响已交付行为的未决 ADR、scope amendment、Steering 同步或下游重验会阻塞 GO。

Skill 负责执行这些阶段门，不定义新的治理内容；治理 owner 和人工审批权不转移给 Skill。

## 2. 同步决策流程

触发事件发生后，按以下顺序判定：

1. **Q1：是否引入新的架构层级、组件或依赖方向？** → 是：更新 structure.md
2. **Q2：是否引入新的技术栈、外部依赖或数据格式？** → 是：更新 tech.md
3. **Q3：是否改变项目定位或核心价值？** → 是：更新 product.md
4. 以上均否 → 不修改 Steering；实现阶段不留下空白检查记录。Feature GO 只在最终 validation/归档事实中记录一次 disposition。

**需要更新的执行步骤：**
1. 标记需更新的 Steering 文件
2. 编写变更（Steering 语言风格：原则性、方向性，不含实现细节）
3. 提交标注 `Steering-sync: {事件名称}`
4. 在 Design 声明的同步任务或 Feature GO evidence 中记录最终 disposition

## 3. ADR ↔ Steering 同步规则

### ADR 获批时
- **tech.md 去重**：检查 tech.md 是否与新 ADR 重叠。如有，删除 ADR 级内容，替换为 `> 📎 详见 ADR-00X`
- **`📎 引用模式`为通用规则**：Steering 不承载 ADR 级决策的原文或理由。引用仅出现在 tech.md 的 Key Technical Decisions 表和 Key Libraries 段落

### ADR 修改时
ADR 理论上不可修改。若发生：视为新增 ADR，旧 ADR 标记"已取代"，Steering 同步按新增流程执行。

## 4. border.md 生命周期与归档

**正式决策：border.md 归入 Steering 扩展层，设置 ADR 提升通道处理全局性约束。**

**生命周期：创建 → 执行 → 归档 → (可选) 提升**

- **创建**：spec 进入 design 阶段时，architect-decision 从 Steering 提取红线，写入 `.kiro/steering/borders/{feature-name}-border.md`
- **执行**：spec 实现期间逐条验证。违反红线 = 架构违规；走出灰线 = 显式记录差异理由
- **归档**：spec 完成时，在 border.md 末尾追加归档标记（日期、关联 spec、约束状态），文件不移动
- **ADR 提升通道**：border.md 中的约束只有在预期变更触及 authority、持久格式、发布/恢复协议、依赖方向或跨 Spec frozen contract 任一语义门槛时，才提升为 ADR candidate。

提升流程：标记候选 → 创建 ADR → 原 border.md 标注 `> 📎 已提升为 ADR-00X`

Spec 引用数、review 数或独立会话数只作为发现长期影响的信号；无论次数多少，都不能代替语义门槛、candidate 说明或人工审批。

### borders/ 目录正式地位

- **Taxonomy**：`StableCognition → Direction` 子类
- **治理关系**：`DerivesFrom` Steering
- **引用规则**：新 spec 可引用已归档 border.md 中的约束作为先例

### 待决项最终决策

| 待决项 | 决策 |
|--------|------|
| 归档时是否需要 Steering 同步检查？ | **是，在 Feature GO 一次闭合**。约束是原则实例化 → 不改 Steering；约束揭示未覆盖领域 → 标记同步候选 |
| ADR 提升触发标准？ | 五类语义门槛：authority、持久格式、发布/恢复协议、依赖方向、跨 Spec frozen contract |
| borders/ 纳入 Taxonomy？ | **是**。`StableCognition → Direction` 子类，`DerivesFrom` Steering |

## 5. 验证与防漂移

**Feature GO 检查**：每个 Spec 只在 Feature GO 按获批 Requirements/Design/Tasks、五类语义门槛与实际实现做一次最终核对，再闭合 Steering 一致性。发现未授权 actual delta 时直接 `NO-GO`；没有 delta 时不追加空记录。task/cluster 内仅在出现实际漂移信号时进入第 2 节流程。

**漂移检测信号：**

| 信号 | 处理 |
|------|------|
| spec 设计与 Steering 描述矛盾 | 触发同步决策流程 |
| 多个 spec 对同一 Steering 条目有不同解释 | 更新 Steering 措辞 |
| ADR 约束与 Steering 原则矛盾 | 以 Steering 为准，评估 ADR 是否需标记"已取代" |

**与规格工作流的集成**：Requirements/Design/Tasks/Feature GO 的 Kiro Skills 必须读取项目治理规则；architect-decision 继续负责设计忠实度与隐式降级拦截。两者都不能自行批准 ADR 或 scope amendment。

---

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-05-16 | 初始版本。解决 Steering 文档静止和 border.md 归属问题。定义 border.md 生命周期和 ADR 提升通道。 |
| v2 | 2026-08-16 | 将 ADR、scope lineage 与 Steering 同步处置接入 cc-sdd 阶段门。 |
| v3 | 2026-08-18 | 将实施期治理收束为无痕语义门；移除引用/会话次数提升与空白同步记录，改由 Feature GO 一次闭合。 |
