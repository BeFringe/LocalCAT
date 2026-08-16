---
name: governance-summary
description: "治理层 Git 提交总结技能。当提交涉及治理体系（Steering、ADR、Spec、治理 Skill、漂移分析、认知基线）时，在 git-summary 基础上叠加治理视角的语义提升约束。触发场景：用户要求总结/润色涉及 .kiro/、steering、ADR、governance、治理基线、演进风险的 commit message；或提交内容属于治理层工件而非功能实现。该技能确保总结站在治理系统设计者的视角输出，避免塌缩为项目审计器或功能实现报告。"
---

# Governance Summary — 治理层提交总结

本技能继承 git-summary 的骨架与纪律，在此基础上叠加治理系统的元约束。
先读完本文件，再按流程执行。

## 元约束：你是在设计治理系统，不是在分析项目

这是唯一的顶层约束，所有后续规则都从它派生。

治理系统是主体，项目是试验场。提交总结应让读者看到"治理能力在生长"，而非"项目在做代码审计"。具体含义：

- 保留项目类型作为背景（长期演化、分层架构、多 Agent 协同等），但不展开项目领域细节
- ADR 举例应泛化为策略类型（runtime storage strategy、subsystem boundary split），而非具体技术选型（JSONL、Trie、Parser）
- 演进风险描述的是治理体系自身的脆弱点，不是项目 bug 清单
- 总结面向"未来需要理解治理演化逻辑的人"，而非"当前项目的 code reviewer"

## 继承规则

以下规则直接继承自 git-summary，不再重复展开：

- Step 0: 识别偏好（minimal_fix / normalization / tight_scope 等）
- Step 1: 提取 commit theme + correction themes + verification facts
- Step 3: 自纠三漂移（Problem drift / Fix drift / Verification drift）
- 轻量模板选择、conventional commit 格式、reviewer-facing 语义

## 治理层增量规则

### 语义提升

在 Step 2（写总结）时，对治理相关内容执行语义提升：

1. **泛化项目特化细节**
   - 具体子系统名 → 抽象策略类型
   - 具体技术选型 → "为什么选择这类方案"的决策模式
   - 具体 feature 实现状态 → 治理能力覆盖范围

2. **简明扼要**
   - 不报数（"7 项风险、5 种漂移模式"对治理系统无意义）
   - 不列举文件名或行数
   - 不写 P0/P1 后续计划（那是 Boundary 的内容，不应出现在 git 总结中）
   - Intent 起笔直接说目的，不要先铺垫问题再引出目的

3. **保持架构演化语义**
   - "为什么某些 subsystem 需要从核心解耦" → 好，这是治理视角
   - "为什么 Parser 要拆分" → 塌回项目细节，需提升

### ADR 描述规范

当提交涉及 ADR 时，描述应反映决策模式的抽象类别，而非具体选择：

```
# 过于项目化
- JSONL append-only 存储
- Layer 2A / 2B 拆分
- Trie 匹配引擎

# 治理视角
- runtime storage strategy
- subsystem boundary split
- stateless orchestration pattern
```

### 背景粒度控制

| 保留 | 弱化 |
|------|------|
| 项目类型（本地优先、长期演化） | 领域细节（CAT、翻译、术语） |
| 分层架构 | 具体层编号与组件名 |
| 多 Agent 治理 | 单个 Agent 行为细节 |
| spec workflow | 单个 spec 内容 |
| cognitive governance | 具体文件内容复述 |

## 示例

**输入**：一个引入 .kiro/steering/ 治理基线的提交，包含 governance-baseline.md、ADR-001~006、evolution-risk-analysis.md

**输出**：

```
governance(kiro): 建立项目级认知治理基线 — 治理纪律的首次显式化

Intent:
  以长期演化的本地优先工具型系统为试验场，首次将治理能力
  从隐性认知显式化为可追溯的纪律体系。

Persistence:
  引入 .kiro/steering/ 治理层：
  - Steering: 项目身份认知 + 红线/灰线约束模型
  - ADR: 从已实施架构中回溯提取不可逆决策（runtime storage strategy、
    subsystem boundary split、state separation 等），补齐治理缺口
  - 演进风险分析: 治理体系自身的脆弱点与漂移模式，使决策背景可追溯

Verification:
  三层治理基线就位，ADR 记录机制含约束与来源追溯。
```

注意这则示例体现的几个要点：
- Intent 直接说目的，不铺垫问题
- ADR 用策略类型举例，不列具体技术名
- 不报文件数、行数、风险数量
- Persistence 和 Verification 都很简短
- 读起来是"治理在生长"，不是"项目在做审计"
