# Governance Understanding Summary v1.1

> Agent 对 LocalCAT 治理模型的理解快照。本文件记录 Agent "如何理解治理"，而非治理规则本身。
> 治理规则见 governance-baseline.md。

---

## 1. 治理模型理解

LocalCAT 的治理系统本质上是一个**双层认知架构**，核心问题是：如何让 AI Agent 在长期迭代中不漂移、不遗忘、不僭越。

### 治理的核心理念

1. **认知先于文件** — Agent 必须先建立理解，再产出物。不是"生成治理文件"，而是"构建治理认知"。
2. **红线不可越** — Steering 中的硬性约束是红线。灰线是红线框架内的有限自由度，不是自由空间。
3. **层级不可混** — Steering ≠ ADR ≠ Spec ≠ Skill。每一层有独立职责，混淆层级是治理漂移的根源。
4. **演进有风险** — 每次扩展（新功能、新技能、新层级）都携带漂移风险，必须识别并记录。

---

## 2. 三层认知模型理解

### Steering = 项目身份认知

**核心理解：** Steering 不是"项目方向"。方向可以微调，身份不能轻易变。Steering 回答的是"这个系统是什么、不做什么"，而非"这个系统要去哪里"。

- **product.md** — 系统身份：local-first CAT tool、100% local、zero telemetry、数据驱动而非功能驱动
- **structure.md** — 架构身份：Layer-first flat-file、4 层严格分离、向下依赖、无状态 LogicController
- **tech.md** — 运行时身份：Python 3.14+、stdlib-only core、frozen dataclass 数据合约
- **border.md**（待建立）— 需求级身份：每次需求从 Steering 提取的红线

**变更条件：** 仅当项目本质变化时。不是功能变更，不是优先级调整。

### ADR = 不可逆决策记忆

**核心理解：** ADR 是永久记录，追加式，不可修改旧记录。

当前缺位。但 tech.md 中已散落多个 ADR 级别决策：
- Trie 用于 Glossary
- JSONL append-only TM
- Stateless LogicController
- TM-priority strategy
- 条件性 openpyxl import

这些是"已做但未记录"的架构决策。

### Spec = 功能生命周期

**核心理解：** Spec 是工作流层，不是治理层。cc-sdd 是功能生命周期的运行时。

Spec 的三大工件：
- requirements.md — 形式化需求（SHALL/WHEN）
- design.md — 组件设计、数据模型、正确性属性
- tasks.md — 实现任务分解

**关键边界：** Spec 中发现的具有全局意义的决策（如 Parser→Engine 单向依赖）应提升为 ADR，不应留在 Spec 中。

### Skill = 治理执行机制

**核心理解：** Skill 是执行者，不是立法者。Skill 不定义治理内容，只执行治理约束。

- **architect-decision** — 忠实度守门：检查实现是否忠实于设计
- **stage-keeper** — 阶段检测：判断当前在哪个阶段，是否漂移
- **omc-reference** — 提交协议：结构化 git trailer

**分工：**
- stage-keeper 管辖"在哪个层级操作"
- architect-decision 管辖"是否忠实于设计"

---

## 3. 红线与灰线理解

### 红线（Red Line）

不可违反的硬约束。跨过红线 = 架构违规。

**必须拦截的场景：**
- Agent 删除设计中显式要求的能力
- 集成验收使用非目标业务 API
- "暂不实现"指向当前步骤的核心能力
- 验证通过但实际结果与验证锚点不符
- 上下文压缩将设计要求改写为实现状态

### 灰线（Gray Line）

红线框架内的有限自由度。**不是自由空间。**

走出灰线 ≠ 违规，但必须显式记录：
1. 原始设计期望是什么
2. 实际实现做了什么
3. 差异的理由

**可允许的灰线场景：**
- Agent 选择设计文档中未指定的合理实现方式
- Agent 的实现与建议不同但功能等价
- "暂不实现"指向不影响验证目标的锦上添花功能

---

## 4. 治理对象关系理解

```
Steering ──governs──→ ADR        (Steering 定义决策框架)
Steering ──governs──→ Spec       (Steering 约束规格边界)
Steering ──constrains→ Skill     (Steering 决定 Skill 的红线/灰线)
ADR ──constrains──→ Spec         (ADR 限制规格可选项)
Spec ──drives──→ Code            (规格驱动实现)
Skill ──enforces──→ Code         (Skill 在实现中执行约束)
Skill ──gates──→ Spec            (architect-decision 守门设计忠实度)

NO RELATIONSHIPS:
  Code ─╳→ Steering              (代码不直接修改 Steering)
  Spec ─╳→ ADR                   (规格不直接产生 ADR)
  Skill ─╳→ Steering             (Skill 不修改 Steering 内容)
```

---

## 5. 演进风险排序理解

### 当前优先级（v1.1）

| 优先级 | 风险 | 理解 |
|--------|------|------|
| 1 | ADR 缺位 🔴 | 不可逆决策散落在 tech.md 和 spec 中，随时可能被静默覆盖 |
| 2 | Steering 文档静止 🔴 | L2A/2B 拆分和 parser 子系统已引入，但 Steering 未同步 |
| 3 | border.md 归属模糊 🔴 | 需求级红线无生命周期管理，约束可能随功能完成而丢失 |
| 4 | Spec-Steering 边界侵蚀 🟡 | design.md 中包含超出功能生命周期的架构决策 |
| 5 | Skill 触发覆盖率不足 🟡 | 未覆盖设计阶段、ADR 提取、Steering 更新 |
| 6 | Steering 演进失控 🟢 | 当前集中治理，缺乏触发条件 |
| 7 | 灰线判定主观性 🟢 | 需先例积累，不阻塞 |

### 关键认知：为什么"文档静止"是高风险

架构已经在演化——structure.md 规划了 L2A/2B 拆分，parser-subsystem spec 已设计了 Layer 2B 的组件架构。但没有任何机制保证这些变化反映回 Steering。Agent 下一次会话可能基于过时的 Steering 做出错误决策。

### 关键认知：为什么"border.md 归属"是高风险

parser-subsystem-extraction 产生了 33 个 correctness properties。这些约束在功能完成后何去何从？如果没有归档机制，它们要么丢失（约束悬空），要么留在已归档的 Spec 中（治理保护缺失）。这是 border.md 生命周期的活例证。

---

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-05-13 | 初始版本 |
| v1.1 | 2026-05-13 | 校准修正：Steering 身份认知定义、灰线修正为有限自由度、风险拆分重排、border.md 优先级恢复 |
