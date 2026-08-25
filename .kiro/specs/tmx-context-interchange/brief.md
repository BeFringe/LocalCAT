# Brief: tmx-context-interchange

## Problem

MateCat/MyMemory 会通过 TMX vendor properties 或 API 参数携带 provenance/context，但不同导出样本并不一致。把未知 `<prop>` 猜成上下文会产生错误排序，完全丢弃又会损失可追溯信息。

“TMX export profile”还容易把三件互相独立的事实混成一个 authority：从哪个业务范围选择双语单元、怎样编码 TMX payload、以及是否把 payload 封装进 ResourcePackage。参考 CAT 平台在项目/job 管理面同时提供译文、原文、XLIFF 和 job TMX 导出，只能作为项目导出分类与交互参考，不能证明这些 artifact 共享 package、identity 或 transaction owner。

## Current State

LocalCAT 已安全导入 TMX Level 1 文本和部分 MateCat 样本，但 canonical TM record、未知 prop 保留、context 映射与 TMX export 尚未形成独立契约。

## Desired Outcome

用户导入受支持的 TMX 时可保留 provenance，并仅在属性名/语义经样本验证后映射上下文；导出时清楚区分 managed TM resource、整个当前项目和当前/明选协作分工，不把缺少 context 的文件误报为损坏，也不把一个范围 owner 误写成另一个 package owner。

managed-resource TMX export 同时提供 direct `.tmx` 与 ResourcePackage 后继 payload profile；该包仍一次只表示一个 managed TM resource 的完整快照，且 container、preview/apply/receipt/cold-reopen 继续由 `language-resource-portability` 拥有。项目/分工 TMX 是由 Workspace/Chunk 签发 exact scope 后生成的直接互操作 artifact，不进入 ResourcePackage。本规格拥有 TMX payload grammar、context/provenance 映射、有损报告和 direct TMX artifact 的验证/发布编排；不拥有 ResourcePackage container、资源 import/apply transaction、Workspace/Chunk scope identity 或 provider 传输。

## Approach

以 profile 驱动的 TMX 互操作层处理标准字段和经验证 vendor props；未知属性作为原始 metadata 保存。该规格消费上游签发的有界、版本化 scope projection，而不是在 Parser 内实现检索，也不从路径、显示名、列表顺序或文本相似度重建资源/项目/chunk 范围。

Export 被拆为三个正交维度：

| 维度 | 可选值/责任 | Authority |
|---|---|---|
| Source scope | `managed_resource` / `entire_project` / `selected_chunk` | Resource / Workspace / Chunk 各自签发 exact snapshot 或 membership projection |
| Payload profile | TMX level、语言、TU/TUV、context/provenance/vendor prop、loss policy | `tmx-context-interchange` |
| Carrier | direct `.tmx`；或仅对 `managed_resource` 可选 ResourcePackage | direct artifact publication / `language-resource-portability` |

三种 scope 只交付足以生成 TMX 的受限事实，不获得 payload 或 carrier authority：

- **Resource**：选中一个 managed TM resource，导出该资源的完整 canonical snapshot；资源页 `⋮` 的 JSONL、TMX 和 ResourcePackage 导出彼此是不同 artifact 操作。
- **Workspace**：签发绑定当前 project/session/revision 的 `entire_project` segment projection；项目、文档、段落身份与导航顺序仍归 Workspace。
- **Chunk**：签发绑定当前 project/session/plan/revision/chunk ID 的完整 exact membership projection，不先过滤 detached，也不携带正文、导航顺序或导出字段。Workspace 再 join 当前 presence/content/order；TMX profile 决定 inclusion，并在 preview 中单独报告 detached 排除数。任何一层都不按位置或文本猜测重绑。LocalCAT 没有独立 Job identity 时，UI 使用“当前分工/所选分工”，不复制参考平台的 Job authority。

TMX profile 再决定哪些已签发单元可 materialize（例如空 target、未确认 target、缺少 context、无法无损表示的 metadata），并生成 inclusion/exclusion/loss report；Resource、Workspace 与 Chunk 不各自实现 TMX writer。

### Export Preview UI Direction

项目下拉只提供一个“导出项目”入口。选择 TMX 后，在同一预览窗口选择“整个项目”或在 active plan 可用时选择一个明选分工。预览视觉可借鉴 ProjectPackage 的卡片层级，但语义保持导出专用：

- 标题与 badge 明示 `PROJECT · 整个项目` 或 `CHUNK · 当前分工`；
- 单行可复制 project ID；chunk scope 另显示 plan/chunk/revision 绑定；
- 独立展示文档数、attached segments、可导出单元、排除单元与 loss/warning；
- 展示源语言/目标语言、TMX payload profile 和目标 `.tmx` 路径；
- “导出”前重新验证 scope 与 destination，stage 后以 TMX reader 冷验证，再单点发布；“取消”不创建或修改目标文件；
- 不显示 ProjectPackage 的 `REPLACE`、reconciliation 或“应用导入”，也不导入/调用 ProjectPackage manifest、carrier 或 receipt owner。

资源页 `⋮` 的 TMX 导出使用独立的 resource-snapshot preview；它不随当前项目/chunk 改变范围。选择“导出资源包”时，只把该 managed resource 的 TMX payload profile 交给 ResourcePackage owner 封装。

## Scope

- **In**: 标准 TMX 文本、语言、TU/TUV 标识、已验证 MateCat/MyMemory props、未知 prop 保留、context/provenance 导入；`managed_resource` / `entire_project` / `selected_chunk` 的受控 export adaptation、preview/loss/round-trip；direct TMX artifact 验证/发布；以及仅限 managed-resource snapshot 的可选 TMX ResourcePackage payload/capability 合同。
- **Out**: ResourcePackage container/manifest authority、资源 import/apply transaction、Workspace/Chunk identity/membership/permission、ProjectPackage、provider 传输、MyMemory 在线 API、所有厂商私有扩展、复杂内联标签的首批编辑、项目文档打开或保存。

## Boundary Candidates

- TMX syntax/profile；
- vendor prop registry；
- canonical TM record、Workspace segment 与 Chunk membership 的受限 adaptation；
- source-scope × payload-profile × carrier capability matrix；
- export inclusion/exclusion/loss report 与 direct artifact publication；
- 导入警告与 provenance 展示。

## Out of Boundary

- 不假定每个 MateCat TMX 都有前后文；
- 不从 `speaker "text"` 外壳猜测任意角色语义；
- 不拥有 fuzzy 算法；
- 不把 project/chunk TMX 冒充为 managed-resource snapshot 或 ResourcePackage；
- 不让 Resource、Workspace 或 Chunk 各自实现一份 TMX grammar/writer；
- 不因参考平台称为 Job 就在 LocalCAT 铸造第二套 Job/Chunk identity。

## Upstream / Downstream

- **Upstream**: Parser 的安全 XML/流式错误语义；SQLite canonical TM records；Multi-Document 的 project/session/revision 与 exact segment/presence/content projection；获批 Chunks 的 plan/revision/full exact membership projection。
- **Downstream**: context-aware suggestions、resource/project/chunk TMX exchange/export；可选地由 `language-resource-portability` 仅将已批准的 managed-resource TMX payload profile 封装进 ResourcePackage。

## Existing Spec Touchpoints

- **Extends**: 已完成的 TMX Level 1 导入。
- **Adjacent**: `tm-storage-retrieval-index` 拥有存储和排序；`multi-document-project-workspace` 拥有 entire-project scope；`collaborative-job-chunks` 只拥有 selected-chunk membership/permission；`language-resource-portability` 拥有单 managed-resource ResourcePackage carrier/transaction。本规格只负责 TMX 互操作映射、loss policy 与 direct artifact publication。

## Constraints

拒绝 DTD/ENTITY 的安全基线不回退；只对真实 fixture 验证过的属性作语义映射；未知属性不得无提示丢失。Export preview 必须绑定 exact source scope identity/revision/digest 与 destination before-fact；detached 必须在 preview 中显式报告并按获批 inclusion policy 处理、不得静默，stale/foreign/unsupported/loss-blocking 才在目标发布前结构化拒绝。项目/分工导出可以复用 ProjectPackage UI 的视觉层级和无语义原子文件原语，但不得复用其 manifest、import/replace/reconciliation authority。
