# 需求文档

## 简介
{{INTRODUCTION}}

<!-- 当范围可能被误读或功能触及相邻系统/规格时必填 -->
## 边界说明
- **范围内**：{{IN_SCOPE_BEHAVIORS}}
- **范围外**：{{OUT_OF_SCOPE_BEHAVIORS}}
- **相邻期望**：{{ADJACENT_SYSTEM_OR_SPEC_EXPECTATIONS}}

<!-- 修订既有规格、重新纳入历史排除范围或依赖相邻规格时必填 -->
### Scope Lineage
- **Owning spec**：{{OWNING_SPEC}}
- **被修订的既有范围说明**：{{EXISTING_SCOPE_REFERENCE_OR_NONE}}
- **相邻规格 / 契约**：{{ADJACENT_SPEC_REFERENCES_OR_NONE}}
- **审批状态**：{{APPROVED_REFERENCE_OR_PENDING}}

## 需求

### Requirement 1：{{REQUIREMENT_AREA_1}}
<!-- Requirement 标题必须使用前置数字 ID；不得使用 Requirement A 等字母 ID。 -->
**目标：** 作为 {{ROLE}}，我希望 {{CAPABILITY}}，以便 {{BENEFIT}}

#### 验收标准
1. When [事件], the [系统] shall [响应 / 行为]
2. If [异常或失败条件], the [系统] shall [响应 / 行为]
3. While [前置状态], the [系统] shall [响应 / 行为]
4. Where [可选能力已纳入], the [系统] shall [响应 / 行为]
5. The [系统] shall [持续成立的行为]

### Requirement 2：{{REQUIREMENT_AREA_2}}
**目标：** 作为 {{ROLE}}，我希望 {{CAPABILITY}}，以便 {{BENEFIT}}

#### 验收标准
1. When [事件], the [系统] shall [响应 / 行为]
2. When [事件] 且 [条件] 成立, the [系统] shall [响应 / 行为]

<!-- 其他 Requirement 沿用相同格式 -->
