# Brief: collaborative-job-chunks

## Problem

未来协作翻译需要把同一项目的任意连续或离散段落分配给不同译者，并能重新划分或合并工作范围。文件、章节和 workbook sheet 是内容结构，不能被协作分工重新定义；如果直接把 Document 当成 chunk，跨章节分工、合并、权限和进度都会污染项目身份。

## Current State

LocalCAT 当前是单机个人编辑器，没有多文档 workspace、chunk、用户身份、权限或实时协作。规划中的 `Project → Document → Segment` 将提供稳定复合段落身份，但只负责内容结构。MateCat 的 split/merge job 行为证明 chunk 可以独立于项目多文件结构随意划分与整合。

## Desired Outcome

项目拥有不改变文档结构的协作 chunk 视图：用户可以按稳定 segment 集合或连续范围创建、拆分、合并、命名和分配 chunk；每个 chunk 显示自己的进度与可编辑范围；取消分工后仍回到同一 Project/Document/Segment 身份。

## Approach

把 chunk 定义为引用稳定 segment 身份的协作对象，而不是新的文档容器。搜索、进度、权限和分配可以消费 chunk scope，但项目保存、source 更新、章节导航与 segment identity 始终由多文档 workspace 掌握。

## Scope

- **In**: chunk 创建、拆分、合并、命名、排序、segment 成员、连续范围、跨文档范围、分配状态、chunk 进度、当前 chunk/全部章节搜索 scope、只读越界浏览、冲突与撤销反馈。
- **Out**: Project/Document/Segment 解析与保存；账号系统；云端传输 provider；实时光标与聊天；计费、外包市场和购买翻译。

## Boundary Candidates

- chunk 使用稳定 segment 引用，不复制 source/target；
- Document 顺序和章节身份不随 chunk 拆分/合并改变；
- chunk 进度只统计其成员，项目进度仍统计整个项目；
- `SearchScope` 可增加 `current_chunk`，但“搜索全部章节”保持项目级语义；
- assignment/permission 与同步传输分离。

## Out of Boundary

- 不把每个文件自动当成一个 chunk；
- 不让 chunk 成为项目保存单元或 TM identity；
- 不把跨端文件同步视为协作锁或实时合并；
- 不在没有稳定多文档 segment identity 前实施。

## Upstream / Downstream

- **Upstream**: `multi-document-project-workspace` 的稳定 Project/Document/Segment、章节导航、source reconciliation 和保存报告。
- **Downstream**: 可选账号/权限、协作审校、chunk 级 QA，以及 `cross-device-sync-plugin` 对 chunk metadata 的可选同步。

## Existing Spec Touchpoints

- **Extends**: 多文档搜索的可扩展 `SearchScope`。
- **Adjacent**: `cross-device-sync-plugin` 只负责传输和冲突，不拥有 chunk 结构或权限。

## Constraints

拆分、合并和重排不得改变任何 segment 的规范身份；越出当前 chunk 的内容若可见必须明确只读；所有成员变化应有可审计报告与安全撤销路径。

## Promotion Clusters

1. chunk identity、稳定 segment membership 与 split/merge 不变量；
2. assignment、permission、progress 与越界只读；
3. Controller scope、搜索 `current_chunk` 与冲突/撤销；
4. Qt 与 current-source acceptance。

该规格必须在 Multi-Document 完整 Cluster 2（C2A aggregation/reconciliation、C2B save/recovery 与 C2C ProjectPackage 手工闭环）完成后启动。跨端同步只有在本规格批准 namespaced chunk metadata 且后续 ProjectPackage schema/version 或明确 extension 同步获批后，才可搬运该 metadata；同步插件不得解释成员资格或权限。
