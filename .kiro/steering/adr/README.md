# LocalCAT 架构决策记录

本目录保存跨 Spec、不可逆或高成本的架构决策。ADR 是追加式决策记忆：已采纳 ADR 的原决策不静默改写；方向变化通过新 ADR 建立明确取代关系。

## 状态语义

- **草案**：候选决策，等待人工审阅；不授权功能实现或 scope 扩张。
- **已采纳**：对后续 Spec 和实现具有约束力。
- **已取代**：保留历史，由新 ADR 接续约束。
- **已拒绝**：保留被否决方案及原因，避免重复决策。

## 当前索引

| ADR | 状态 | 主题 |
|---|---|---|
| ADR-001 | 已采纳 | Glossary 匹配结构 |
| ADR-002 | 已采纳 | legacy TM 存储与冲突策略 |
| ADR-003 | 已采纳 | Excel 运行时依赖隔离 |
| ADR-004 | 已采纳 | Parser 与 Engine 依赖方向 |
| ADR-005 | 已采纳 | Parser 抽象入口 |
| ADR-006 | 已采纳 | legacy TM 精确索引策略 |
| ADR-007 | 草案 | SQLite canonical TM 与 JSONL 兼容边界 |
| ADR-008 | 草案 | sealed/active 内容证明发布链 |
| ADR-009 | 草案 | 证据门与 fail-closed 能力发布 |
| ADR-010 | 草案 | 有界 fuzzy 候选证明 |
| ADR-011 | 草案 | Feature 5 与 UI 的冻结合同集成 |

新记录使用 `.kiro/settings/templates/adr.md`。创建、取代和 Steering 同步遵循 `.kiro/settings/rules/governance.md` 与 `../steering-sync-mechanism.md`。
