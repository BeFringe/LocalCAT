# Steering 同步机制

> 确保 Steering 文档（product.md / structure.md / tech.md）与项目实际架构保持一致。
> 解决治理基线 R2（Steering 文档静止）和 R3（border.md 归属模糊）。

---

## 1. 触发事件清单

| 事件 | 触发时机 | 影响范围 |
|------|----------|----------|
| Spec design review 完成 | design.md 通过评审时 | structure.md / tech.md |
| ADR 新增 | 新 ADR 创建后 | tech.md（去重） |
| Spec 归档 | 功能完成，spec 归档时 | structure.md |
| border.md 归档 | 功能完成后 | steering/borders/ |
| 架构重构 | 改变层级结构或依赖方向 | product / structure / tech |
| Steering 引用冲突 | 实现中发现 Steering 与实际不符 | 涉及的具体文件 |

## 2. 同步决策流程

触发事件发生后，按以下顺序判定：

1. **Q1：是否引入新的架构层级、组件或依赖方向？** → 是：更新 structure.md
2. **Q2：是否引入新的技术栈、外部依赖或数据格式？** → 是：更新 tech.md
3. **Q3：是否改变项目定位或核心价值？** → 是：更新 product.md
4. 以上均否 → 记录"已检查，无需更新"

**不更新的记录格式**（追加在 spec 或 ADR 文件末尾）：

```markdown
---
## Steering 同步检查
- **日期**：YYYY-MM-DD
- **触发事件**：{事件名称}
- **检查结果**：无需更新
- **理由**：{1-2 句话}
```

**需要更新的执行步骤：**
1. 标记需更新的 Steering 文件
2. 编写变更（Steering 语言风格：原则性、方向性，不含实现细节）
3. 提交标注 `Steering-sync: {事件名称}`
4. 在触发文件中追加同步检查记录

## 3. ADR ↔ Steering 同步规则

### ADR 新增时
- **tech.md 去重**：检查 tech.md 是否与新 ADR 重叠。如有，删除 ADR 级内容，替换为 `> 📎 详见 ADR-00X`
- **`📎 引用模式`为通用规则**：Steering 不承载 ADR 级决策的原文或理由。引用仅出现在 tech.md 的 Key Technical Decisions 表和 Key Libraries 段落

### ADR 修改时
ADR 理论上不可修改。若发生：视为新增 ADR，旧 ADR 标记"已取代"，Steering 同步按新增流程执行。

## 4. border.md 生命周期与归档（R3 决策）

**正式决策：方案 B + ADR 提升通道。** border.md 归入 Steering 扩展层，设有 ADR 提升通道处理全局性约束。

**生命周期：创建 → 执行 → 归档 → (可选) 提升**

- **创建**：spec 进入 design 阶段时，architect-decision 从 Steering 提取红线，写入 `.kiro/steering/borders/{feature-name}-border.md`
- **执行**：spec 实现期间逐条验证。违反红线 = 架构违规；走出灰线 = 显式记录差异理由
- **归档**：spec 完成时，在 border.md 末尾追加归档标记（日期、关联 spec、约束状态），文件不移动
- **ADR 提升通道**：border.md 中的约束满足以下任一条件时提升为 ADR 候选：
  1. 被后续 ≥2 个 spec 引用为红线
  2. 被一次架构变更确认具有全局意义
  3. 被不同会话独立引用 ≥2 次

提升流程：标记候选 → 创建 ADR → 原 border.md 标注 `> 📎 已提升为 ADR-00X`

### borders/ 目录正式地位

- **Taxonomy**：`StableCognition → Direction` 子类
- **治理关系**：`DerivesFrom` Steering
- **引用规则**：新 spec 可引用已归档 border.md 中的约束作为先例

### 待决项最终决策

| 待决项 | 决策 |
|--------|------|
| 归档时是否需要 Steering 同步检查？ | **是**。约束是原则实例化 → 无需更新；约束揭示未覆盖领域 → 标记同步候选 |
| ADR 提升触发标准？ | 三条任一：(1) ≥2 spec 引用 (2) 架构变更确认 (3) 独立引用 ≥2 次 |
| borders/ 纳入 Taxonomy？ | **是**。`StableCognition → Direction` 子类，`DerivesFrom` Steering |

## 5. 验证与防漂移

**定期检查**：每完成一个 spec 后触发 Steering 一致性检查（第 2 节决策流程）。

**漂移检测信号：**

| 信号 | 处理 |
|------|------|
| Steering 条目在最近 N=3 个 spec 中未被引用为红线 | 标记"待验证"，下一轮主动评估 |
| spec 设计与 Steering 描述矛盾 | 触发同步决策流程 |
| 多个 spec 对同一 Steering 条目有不同解释 | 更新 Steering 措辞 |
| ADR 约束与 Steering 原则矛盾 | 以 Steering 为准，评估 ADR 是否需标记"已取代" |

**与 architect-decision 的集成**：如果当前会话涉及触发事件，architect-decision 在 Session Requirement Template 中追加"Steering 同步检查需求"。本机制不修改 Skill 行为规则，仅在触发条件层面增加检查点。

---

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-05-16 | 初始版本。解决 R2 + R3。定义 border.md 生命周期和 ADR 提升通道。 |
