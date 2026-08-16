# LocalCAT Spec 治理规则

本规则把 Steering 与 ADR 的治理检查嵌入 cc-sdd 各阶段。它约束规格工作流，但不替代 Steering、ADR 或人工审批。

## 权威与所有权

- Steering 定义项目身份、长期原则与跨线边界。
- ADR 记录不可逆或高成本的架构选择；已采纳 ADR 只通过新 ADR 取代，不静默改写原决策。
- Spec 定义单个功能的 Requirements、Design 与 Tasks；Spec 可以提出 ADR 候选，但不能自行批准项目级决策。
- Skill 执行本规则，不成为新的治理权威。
- `.kiro/steering/**`、`.kiro/settings/**` 与项目级工作流只在治理分支形成一次提交，再由功能分支继承。

## ADR 候选触发条件

满足任一条件时，Design 必须记录 ADR 候选或引用既有 ADR：

1. 决策改变 canonical authority、持久化格式、schema、发布/恢复协议或依赖方向；
2. 决策影响两个及以上 Spec，或在当前 Spec 完成后仍长期约束后续实现；
3. 替换、收窄或冲突于已采纳 ADR、Steering 红线或跨线 frozen contract；
4. 回退成本高，且仅靠当前 Spec 无法完整解释其长期后果。

普通局部实现选择、可逆重构和仅在当前任务内有效的细节不创建 ADR。

## cc-sdd 阶段检查

### Requirements

- 当功能修改既有 Spec、重新纳入曾明确排除的能力，或依赖相邻 Spec 时，填写 `Scope Lineage`。
- 明确 owning spec、被修订的既有范围声明、相邻期待和人工批准状态。
- Requirements 只描述可观察范围；不得在此批准架构实现。

### Design

- 必须填写 `Governance Impact`，即使结论是“无 ADR/Steering 影响”。
- 列出适用 Steering、既有 ADR、候选 ADR、取代关系及 Steering 同步结论。
- 新 ADR 或取代关系尚未获得人工批准且会改变本次实现边界时，Design 为 `NO-GO`。
- ADR 草案使用 `.kiro/settings/templates/adr.md`，并由治理分支拥有；功能分支只记录候选和引用。

### Tasks

- 若 Design 要求 ADR、Steering 或跨 Spec 同步，任务图必须包含显式治理闭合项、owner、依赖和可观察完成条件。
- 无治理影响时不得制造空洞的治理任务；保留 Design 中的“无需同步”结论即可。

### Implementation 与 Feature GO

- 实现不得把未批准的 ADR 候选当作授权，也不得从代码既成事实反推治理批准。
- 若已交付行为仍存在未决 ADR、未完成 Steering 同步、未批准 scope amendment 或未重验的下游触发项，Feature GO 必须拒绝。
- Feature GO 只接受当前提交上的 fresh evidence；历史报告或对话结论不能替代。

## 最小治理处置格式

```markdown
## Governance Impact
- **Applicable Steering**: ...
- **Applicable ADRs**: ADR-NNN / None
- **ADR disposition**: Follow existing / New candidate / Supersede / None
- **Scope amendment**: Approved reference / Not required / Pending
- **Steering sync**: Required (targets) / Not required (reason)
- **Downstream revalidation**: ... / None
```
