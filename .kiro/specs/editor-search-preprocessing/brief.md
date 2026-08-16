# Brief: editor-search-preprocessing

## Stage Positioning

本规格不再拥有“先让 Qt 出现一个可用搜索栏”的第一阶段。单 JSON 的基础 source/target/speaker 搜索、结果导航、target-only 预处理预览/应用和 disabled 的 Match Case / Whole Word 入口，归 `qt-editor-json-mvp-increment`。

本规格保留第二阶段：Feature 5 的 `SearchOptions` / `TextMatcher` 契约合并后，启用 Match Case / Whole Word、统一 Unicode/CJK 命中语义、扩展大项目结果与性能边界。它不得在 Qt 或本规格中复制 case-fold、词界或 fuzzy 算法。

多文档 workspace 落地后，搜索 UI 使用“当前章节 / 搜索全部章节”。内部查询使用可扩展 scope，为未来 `current_chunk` 留接口；chunk 未实现前不得把章节布尔值持久化为 chunk，也不得显示虚假的协作范围。

## Problem

长项目目前只能靠段落滚动定位关键词，且反复出现的简单文本清理只能在文件外处理；这会打断翻译位置、增加误改风险。

## Current State

编辑器已有稳定 segment id、最近位置、source/target 浏览页和术语高亮，但没有项目级搜索，也没有受控的预处理规则与差异预览。

## Desired Outcome

用户可按 Match Case / Whole Word 快速查找 source、target 和 speaker，逐个导航结果；用户也可预览一组 target-only 简单预处理规则的影响，在明确确认后应用，并能保存项目而不破坏 segment 身份。

## Approach

搜索先作为只读索引/导航能力；预处理先限定为有顺序的文字替换规则，必须预览命中段与变更差异后显式应用。正则、脚本和 Replace All 不进入首批。

## Scope

- **In**: source/target/speaker 搜索范围、Match Case、Whole Word、结果计数/高亮/前后导航、无结果反馈；target-only 有序文字替换规则、启停、预览、显式应用、撤销边界与未保存状态。
- **Out**: 在线搜索、语义搜索、全局文件系统搜索、搜索即替换、自动正则脚本、翻译优化/机器改写。

## Boundary Candidates

- Unicode 文本匹配器；
- 项目搜索 session 与结果定位；
- 预处理 rule record；
- preview/apply report；
- Qt 搜索栏与设置管理器。

## Out of Boundary

- 搜索 Match Case / Whole Word 不修改术语记录；
- 预处理默认不在打开项目时自动运行；
- 预处理不修改 source、segment id 或 speaker_id；target 实际变化撤销确认。
- 项目升级、重新导入 source 差异和段落重关联属于 Parser / multi-document reconciliation，不属于文字预处理。

## Upstream / Downstream

- **Upstream**: `qt-editor-json-mvp-increment` 提供基础搜索/预处理产品闭环；`tm-storage-retrieval-index` 提供统一匹配契约。Parser 只在未来更换段落来源时触发重新验证，不阻塞单 JSON 阶段。
- **Downstream**: 校对、术语一致性和未来替换功能。

## Existing Spec Touchpoints

- **Extends**: Qt 主窗口、浏览/校对和工作区状态。
- **Adjacent**: `glossary-management` 共享用户术语，但不共享搜索 session。

## Constraints

短查询和中日韩文本的 Whole Word 语义必须在需求/设计中明确定义；预处理必须先展示受影响段数和具体差异。
