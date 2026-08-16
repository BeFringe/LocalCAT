# Brief: speaker-display-profiles

## Stage Positioning

原始 speaker 的独立显示先由 `qt-editor-json-mvp-increment` 完成；本规格只拥有后续的显示名、显式留空、头像和按项目 profile 持久化。两阶段都不得改写 raw speaker、项目导出或 TM identity。

## Problem

项目记录已经保存 `speaker`，但三栏编辑器和浏览/校对页没有独立展示，译者只能从正文包装或上下文猜测说话者，也无法为反复出现的角色配置更易读的显示方式。

## Current State

JSON 项目能读取/保存 `speaker`，TM 兼容桥也会使用原始 speaker 做严格精确匹配；左侧段落、SOURCE/TARGET 编辑区和四栏浏览表均未单独呈现 speaker。

## Desired Outcome

原始 speaker 在编辑与预览中有稳定对齐位置；用户可按项目把 `NVLHED` 显示为“旁白”、显式留空或用小头像代替文字，同时匹配、保存与导出仍使用原始 speaker。

## Approach

把不可变的 `speaker_id` 与纯显示 profile 分离。profile 只影响 Qt 呈现，并提供原始身份的 tooltip/可访问名称；头像使用本地受管副本和安全回退。

## Scope

- **In**: 项目 speaker 汇总、显示名编辑、显式空白、头像选择/移除、编辑/左栏/浏览/建议一致显示、无头像/丢失头像回退、对齐和可访问性。
- **Out**: 修改原始 speaker、把别名写入 TM key、角色识别 AI、远程头像、动画立绘、TTS。

## Boundary Candidates

- 原始 speaker 身份契约；
- 每项目 display profile 持久化；
- 头像托管与失效回退；
- Qt speaker cell/avatar renderer；
- TM suggestion 的 speaker/context 展示适配。

## Out of Boundary

- profile 不进入 JSON/PO/RPY/XLIFF 源文件；
- profile 不影响 exact/fuzzy 评分；
- 空白显示不删除 speaker，只保留空的对齐列。

## Upstream / Downstream

- **Upstream**: `qt-editor-json-mvp-increment` 的 raw speaker 展示和 Qt 工作区状态；Parser/codec 未来迁移时只需继续保留 speaker。
- **Downstream**: 快速搜索 speaker、角色一致性检查、未来项目包导出。

## Existing Spec Touchpoints

- **Extends**: Qt 编辑器的段落列表、当前段和浏览/校对显示。
- **Adjacent**: `tm-storage-retrieval-index` 使用原始 speaker 作为上下文，不读取显示 profile。

## Constraints

默认显示原始 speaker；建议 profile 按项目保存。显式空白与“未配置”必须可区分，头像丢失时仍可恢复原始身份。
