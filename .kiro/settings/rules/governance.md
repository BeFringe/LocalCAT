# LocalCAT Spec 治理规则

本规则把 Steering 与 ADR 的治理检查嵌入 cc-sdd 各阶段。它约束规格工作流，但不替代 Steering、ADR 或人工审批。

## 权威与所有权

- Steering 定义项目身份、长期原则与跨线边界。
- ADR 记录不可逆或高成本的架构选择；已采纳 ADR 只通过新 ADR 取代，不静默改写原决策。
- Spec 定义单个功能的 Requirements、Design 与 Tasks；Spec 可以提出 ADR 候选，但不能自行批准项目级决策。
- Skill 执行本规则，不成为新的治理权威。
- `.kiro/steering/**`、`.kiro/settings/**` 与项目级工作流只在治理分支形成一次提交，再由功能分支继承。

## ADR 候选语义门槛

ADR 不按 review、cluster、Spec 引用或会话次数产生。预期变更先按下列语义门槛判定；跨过门槛但已由获批 Design 精确映射到既有 adopted ADR、且没有超出其边界时，按既有授权继续。其余跨门槛变化必须停止实现，并回到 Design 记录 ADR candidate 与人工审批要求：

1. 改变 canonical authority、授权铸造/撤销边界或 source of truth；
2. 改变持久格式、schema、migration 或兼容读取合同；
3. 改变发布/恢复协议、原子性、durability 或 fail-closed 失败语义；
4. 改变依赖方向、层级责任或 composition boundary；
5. 改变跨 Spec frozen contract、长期 ownership 或已采纳 ADR/Steering 红线。

普通局部实现选择、可逆重构、对既有合同的缺陷修复和仅在当前任务内有效的细节不创建 ADR。引用次数、review 次数和独立会话次数只能帮助发现可能的长期影响，不能单独触发或批准 ADR。

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
- 每个 task/cluster 准备时静默核对上述五类语义门槛；未触发时继续实施，不追加“已检查/无影响”记录，也不制造 governance disposition 流水账。
- 一旦预期变更跨过门槛，先核对获批 Design 是否已精确映射到能够完整授权该变化的 adopted ADR；映射闭合且未越界时继续，否则立即停止相关实现，写入 ADR candidate、影响面与人工审批要求。在批准前不得用 Implementation Note 代替授权。
- `Implementation Notes` 只保留会改变后续任务判断的信息增量，例如新的反例、恢复的合同、明确 deferred boundary、复现前提或验证约束；例行测试计数、审批复述与“无 ADR/Steering 影响”不单独记账。
- 若已交付行为仍存在未决 ADR、未完成 Steering 同步、未批准 scope amendment 或未重验的下游触发项，Feature GO 必须拒绝。
- Feature GO 必须对最终 diff、tree 与 runtime 相对获批 Requirements/Design/Tasks 再执行一次五类语义门槛核对；任何实施期漏报或未授权的实际 delta 均为 `NO-GO`，不得因尚未建立 candidate 而放行。没有实际 delta 时不生成空白 `None` 记录。
- Steering 在 Design 记录预期同步面；Feature GO 依据实际 tree/runtime 一次闭合最终 disposition。实现 cluster 不重复生成空白同步记录。
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
