# Governance Cognitive Baseline v1

> LocalCAT 项目治理认知基线。定义治理系统的定位、认知模型、风险分析和对象分类。
> 本文档是治理系统的身份锚点，不是功能规格。

---

## 1. 治理系统定位

### 双层认知架构

LocalCAT 治理系统是一个**双层认知架构**，核心问题是：如何让 AI Agent 在长期迭代中不漂移、不遗忘、不僭越。

```
                    ┌─────────────────────────┐
                    │    Governance Layer      │
                    │  (长期架构认知 + 决策记忆)  │
                    │  Steering / ADR / Skill  │
                    └──────────┬──────────────┘
                               │ 治理约束向下传递
                    ┌──────────▼──────────────┐
                    │    cc-sdd Workflow       │
                    │  (功能生命周期运行时)       │
                    │  requirements → design   │
                    │  → tasks → implementation│
                    └──────────┬──────────────┘
                               │ 规格驱动实现
                    ┌──────────▼──────────────┐
                    │  Feature Implementation │
                    │  (代码、测试、基准)        │
                    └─────────────────────────┘
```

### 治理的核心理念

1. **认知先于文件** — Agent 必须先建立理解，再产出物。不是"生成治理文件"，而是"构建治理认知"。
2. **红线不可越** — Steering 中的硬性约束是红线。实现细节是灰线，灰线是红线框架内的有限自由度。
3. **层级不可混** — Steering ≠ ADR ≠ Spec ≠ Skill。每一层有独立职责，混淆层级是治理漂移的根源。
4. **演进有风险** — 每次扩展都携带漂移风险，必须识别并记录。

---

## 2. 三层认知模型

### 层级职责分离

| 层级 | 职责 | 生命周期 | 变更频率 | 代表物 |
|------|------|----------|----------|--------|
| **Steering（领航层）** | 项目身份认知：系统是什么、不做什么、架构原则与运行时边界 | 长期稳定 | 极低（仅当项目本质变化时） | product.md / structure.md / tech.md |
| **ADR（架构决策记录）** | 不可逆或高成本的架构决策 | 永久记录 | 低（重大变更时新增，不修改旧记录） | （待建立） |
| **Spec（规格层 / cc-sdd）** | 功能生命周期：需求→设计→任务 | 功能级 | 中（每个功能一组，完成后归档） | .kiro/specs/*/ |

### 层级间判定规则

**Steering vs ADR：**
- 如果一个决策改变了"项目是什么" → Steering 变更
- 如果一个决策选择了"不可逆的技术路径" → ADR
- Steering 定义原则方向，ADR 记录具体选择

**ADR vs Spec：**
- 如果一个约束在当前功能完成后仍然有效 → ADR 候选
- 如果约束仅在功能开发期间有效 → Spec
- Spec 中发现的具有全局意义的属性应提升为 ADR

**Spec vs Skill：**
- Spec 约束功能实现
- Skill 约束 Agent 行为
- Skill 是治理执行者，不是治理立法者

### 治理对象的当前状态

| 对象 | 状态 | 说明 |
|------|------|------|
| Steering: product.md | ✅ 已存在 | 项目定位、核心价值、不做什么 |
| Steering: structure.md | ✅ 已存在 | 分层架构、依赖方向、命名规则 |
| Steering: tech.md | ✅ 已存在 | 技术栈、运行时边界、外部依赖红线 |
| Steering: border.md | ⚠️ 未建立 | 需求级红线的生命周期未治理 |
| ADR | ❌ 未建立 | tech.md 中散落着 ADR 级别决策，需提取 |
| cc-sdd: parser-subsystem-extraction | ✅ 已存在 | 14 需求 / 903 行设计 / 20 任务 |
| Skill: architect-decision | ✅ 已存在 | 设计忠实度守门 |
| Skill: stage-keeper | ✅ 已存在 | 阶段/漂移检测 |
| Skill: omc-reference | ✅ 已存在 | 提交协议规范 |

---

## 3. 红线与灰线

### 定义

**红线（Red Line）：** 不可违反的硬约束。跨过红线 = 架构违规。

**灰线（Gray Line）：** 允许实现差异但必须在红线框架内的软约束。走出灰线 ≠ 违规，但需显式记录差异理由。

### 判定规则

**必须拦截（红线违规）：**
- Agent 删除设计中显式要求的能力
- 集成验收使用非目标业务 API
- "暂不实现"指向当前步骤的核心能力
- 验证通过但实际结果与验证锚点不符
- 上下文压缩将设计要求改写为实现状态

**可允许（灰线空间）：**
- Agent 选择设计文档中未指定的合理实现方式
- Agent 的实现与建议不同但功能等价
- "暂不实现"指向不影响验证目标的锦上添花功能

**灰线行动要求：** 走出灰线时，必须显式记录：
1. 原始设计期望是什么
2. 实际实现做了什么
3. 差异的理由

### LocalCAT 实例

| 约束 | 类型 | 来源 |
|------|------|------|
| L1-L3 只用 stdlib | 红线 | tech.md |
| 所有 .py 在项目根目录 | 红线（当前） | structure.md |
| 严格的向下依赖（L4→L3→L2→L1） | 红线 | structure.md |
| LogicController 无状态 | 红线 | product.md |
| 数据合约使用 frozen dataclass | 红线 | tech.md |
| Parser → Engine 单向依赖 | 红线 | spec/design.md |
| Trie 用于 Glossary | 灰线→ADR 候选 | tech.md（记录为决策） |
| 具体文件命名 | 灰线 | structure.md |

---

## 4. 演进风险分析 v1.1

| 优先级 | 风险 | 严重度 | 触发条件 |
|--------|------|--------|----------|
| 1 | ADR 缺位 | 🔴 高 | 不可逆决策散落在 tech.md 和 spec 中，无治理保护 |
| 2 | Steering 文档静止 | 🔴 高 | 架构已演化（L2A/2B 拆分、parser 子系统），Steering 缺乏同步更新机制 |
| 3 | border.md 归属模糊 | 🔴 高 | 需求级红线生命周期未治理，存在约束丢失风险 |
| 4 | Spec-Steering 边界侵蚀 | 🟡 中 | Spec 承载了超出 feature 生命周期的架构决策 |
| 5 | Skill 触发覆盖率不足 | 🟡 中 | 治理机制未覆盖全生命周期（设计阶段、ADR 提取、Steering 更新） |
| 6 | Steering 演进失控 | 🟢 低 | 当前主 Agent 集中治理，缺乏触发条件 |
| 7 | 灰线判定主观性 | 🟢 低 | 需先例积累，不阻塞当前阶段 |

### 漂移模式

1. **静默覆盖（Silent Override）** — 无显式决策记录的架构变更。最常见于 ADR 缺位时。
2. **层级混淆（Layer Confusion）** — Spec 承载了 ADR 或 Steering 级内容，或 Skill 尝试定义治理内容。
3. **隐式降级（Implicit Downgrade）** — 以"暂不实现"为由降级设计要求，不产生决策记录。
4. **反向依赖（Reverse Dependency）** — 治理工件的修改依赖实现代码验证，形成反向耦合。
5. **跨会话解释漂移（Cross-Session Interpretation Drift）** — 不同会话对同一 Steering 条款产生不同解释。

---

## 5. 治理对象分类

### 治理对象分类树

```
GovernanceObject
├── StableCognition          # 稳定认知（低频变更）
│   ├── Direction            # 方向：项目定位、核心价值、不做清单
│   ├── ArchitecturePrinciple# 架构原则：分层、依赖方向、命名
│   └── RuntimeBoundary      # 运行时边界：技术栈、外部依赖红线
│
├── DecisionRecord           # 决策记录（追加式）
│   ├── ArchitectureDecision # 架构决策：不可逆选择
│   └── TradeoffRecord       # 权衡记录：可逆但有成本的选择
│
├── FeatureSpecification     # 功能规格（功能级生命周期）
│   ├── FormalRequirement    # 形式化需求（SHALL/WHEN）
│   ├── ComponentDesign      # 组件设计
│   └── ImplementationPlan   # 实现计划
│
└── AgentConstraint          # Agent 约束（会话级行为规则）
    ├── FidelityGate         # 忠实度守门
    ├── PhaseDetector        # 阶段检测
    └── ProtocolSpec         # 协议规范
```

### 治理关系分类

```
GovernanceRelation
├── Governs          # 上级治理对象约束下级
│   Steering → ADR
│   Steering → Spec
│   ADR → Spec
│
├── Enforces         # Agent 约束在实现中执行治理规则
│   Skill → Code
│   Skill → Spec
│
├── DerivesFrom      # 下级从上级派生
│   Spec ← Steering
│   border.md ← Steering
│
└── Records          # 决策记录的关系
    ADR ← Steering（决策在 Steering 框架内做出）
```

### 治理术语表

| 中文术语 | English | 定义 | 所属层级 |
|----------|---------|------|----------|
| 领航文档 | Steering | 项目长期稳定的认知锚点 | Steering |
| 架构决策记录 | ADR | 不可逆或高成本架构决策的永久记录 | ADR |
| 功能规格 | Spec (cc-sdd) | 单功能生命周期的形式化描述 | cc-sdd |
| 红线 | Red Line | 不可违反的硬约束 | Steering |
| 灰线 | Gray Line | 红线框架内的有限自由度，走出需显式记录 | Steering |
| 治理漂移 | Governance Drift | Agent 行为偏离治理约束 | 跨层 |
| 忠实度 | Fidelity | 实现对设计规格的遵守程度 | Skill |
| 阶段守门 | Phase Gate | 防止过早下钻到下一阶段 | Skill |
| 静默覆盖 | Silent Override | 无显式决策记录的架构变更 | 漂移模式 |
| 需求级红线 | Per-Requirement Red Line | 每次需求从 Steering 提取的硬约束 | border.md |
| 治理授权 | Governance Authorization | Steering 对 Skill 行为的显式授权 | Steering→Skill |
| 认知锚点 | Cognitive Anchor | 防止理解漂移的固定参照物 | Steering |
| 身份认知 | Identity Cognition | 项目对"自己是什么"的稳定理解 | Steering |

### 分类学使用规则

1. **新对象必须映射到分类** — 任何新增的治理工件必须明确其 TaxonomyNode 归属。
2. **关系不可反向** — `Governs` 是单向的（上→下）。`DerivesFrom` 是单向的（下→上认来源）。
3. **术语统一** — 治理讨论中使用术语表中的标准术语，避免同义异名。

---

## 6. 下一步动作优先级

### P0（立即）

1. **建立 ADR 机制** — 从 tech.md 和 parser-subsystem spec 中提取已有架构决策
   - 候选：Trie for Glossary / JSONL append-only TM / Stateless LogicController / 单向依赖 / TM-priority strategy
2. **定义 border.md 生命周期** — 谁创建、何时创建、何时归档、归档到哪

### P1（本迭代内）

3. **同步 Steering 与当前架构** — structure.md 需反映 L2A/2B 拆分和 parser 子系统
4. **审查 Spec 中的 ADR 级内容** — parser-subsystem design.md 的 33 correctness properties 中哪些应提升为 ADR

### P2（基线稳定后）

5. **评估 Skill 触发覆盖率** — architect-decision 是否覆盖设计阶段和 ADR 提取
6. **建立灰线先例库** — 为灰线判定提供定量参考

---

## 版本记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-05-13 | 初始基线。基于初版认知产物 + 用户校准修正（Steering 身份认知定义、风险拆分重排、border.md 优先级恢复、灰线定义修正） |
