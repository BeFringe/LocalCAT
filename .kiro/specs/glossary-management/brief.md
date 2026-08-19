# Brief: glossary-management

## Stage Positioning

本规格不再重复实现 Qt 已完成的术语管理基线。`qt-editor-json-mvp-increment` 的 4.5(P)/4.5a/4.7a 已交付集中式列表、新增、编辑、删除、冲突反馈、原子事务、Trie/配置 matcher 热重载，以及主窗口 Termbase 页和资源设置菜单的两个入口；新 v1 记录已持久化 Match Case / Whole Word，旧两列记录保持既有语义。

本规格后续只拥有基线之上的批量选择、管理器内检索/排序、注释与来源、扩展 CSV/XLSX 互操作和导出/QA。Feature 1 的 Trie 重叠匹配与长词优先仍是独立兼容能力，不并入 Feature 5。

## Problem

当前用户已经能集中查看并增删改术语，也能为 v1 记录配置 Match Case / Whole Word；仍缺少大词表下的管理器内检索、批量操作、排序、注释/来源与完整扩展导入导出。

## Current State

`TermbaseStore`、Controller transaction 与 `QtTermbaseDialog` 已形成单一 CRUD/恢复路径；v1 记录保存匹配 flags，legacy 两列仍由逐字符 Trie 保持区分大小写、允许子串、支持重叠并由 UI 最长优先。当前互操作仍以已批准的 legacy/v1 输入为主，尚无批量管理与完整扩展导出。

## Desired Outcome

用户可在本地术语管理器中完整维护术语记录，为每条记录设置大小写与全词语义，立即看到当前段命中变化，并安全导入/导出兼容文件。

## Approach

先定义版本化术语记录和明确的 Unicode 匹配语义，再让管理 UI、导入器和匹配引擎共同使用同一记录。两列 CSV 保留兼容路径，扩展列采用有版本/表头的显式格式。

## Scope

- **In**: 列表/搜索、增删改、批量选择、Match Case、Whole Word、注释/来源、排序、即时热重载、CSV/XLSX 兼容导入与扩展导出、冲突/重复反馈。
- **Out**: 云端词库、词形还原、术语自动翻译、术语 QA 批处理、在线共享。

## Boundary Candidates

- Term record 与匹配标志；
- Unicode casefold 与 word-boundary 规则；
- 旧两列/新扩展列导入兼容；
- termbase store 的原子 CRUD；
- Qt 管理器与 EditorController use cases。

## Out of Boundary

- 不让 Qt 直接编辑 CSV/数据库；
- 不把项目快速搜索选项保存为术语属性；
- 不静默改变旧术语命中默认。

## Upstream / Downstream

- **Upstream**: `qt-editor-json-mvp-increment` 的基础 CRUD/热重载、当前 Trie 命中与资源 Active/Lookup/Update 语义，以及 `tm-storage-retrieval-index` 的兼容文本匹配契约。
- **Downstream**: 源文高亮、插入译文、未来术语 QA。

## Existing Spec Touchpoints

- **Extends**: Qt 语言资源和术语建议闭环。
- **Adjacent**: Parser 负责文件格式读取；本规格拥有用户可见术语记录和匹配行为。

## Constraints

需求阶段必须裁决旧两列记录的默认语义。建议迁移时维持当前“区分大小写 + 子串”，新建记录可采用不同的 UI 默认，但两者都必须对用户可见且可修改。
