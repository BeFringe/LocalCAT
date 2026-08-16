# Brief: multi-document-project-workspace

## Problem

LocalCAT 当前把一个项目等同于一个文件和一条扁平段落序列，无法稳定表达“一个翻译项目包含多个章节文档”。如果文件夹中的多个 JSON、XLSX 的多个工作表或一组 RPY 直接共用当前模型，章节身份、段落 ID、显示顺序和保存失败边界都会混在格式解析与 UI 中。

## Current State

当前 `EditorProject` 只有单个绝对 `path` 与项目内唯一的 `segments`，`editor_project.py` 只打开 JSON/TXT 并统一保存为 LocalCAT JSON。这个模型足以继续服务现有单 JSON 项目，不要求本规格先落地。Parser 重新基线正在定义 purpose-aware codec、中立 `ParsedSegment` 与格式 metadata，但不应同时拥有跨文档工作区聚合。

## Desired Outcome

LocalCAT 以稳定的 `Project → Document → Segment` 层级打开和导航多章节项目：项目来源可以是单文件、目录或 workbook；章节显示名与顺序不改变持久身份；每个可写格式仍由自己的 codec/sidecar 负责回写；保存成功、部分失败与恢复状态对用户可见且不会静默丢失编辑。

## Approach

在 Parser/Codec 与 EditorController 之间增加格式中立的多文档工作区聚合。`ProjectOrigin` 描述 `single_file`、`directory` 或 `workbook` 来源；`ProjectDocument` 保存稳定文档身份、来源引用、显示信息、codec 能力和段落；项目内段落身份由稳定的 `(document_id, local_segment_id)` 复合键形成。格式专属 token、原始字节映射和 writer sidecar 留在 codec 边界，不进入通用 UI 契约。

MateCat 参考中的 file navigation 与本规格的 Document/Chapter 接近；MateCat 的 chunk 是可拆分、合并并分配给译者的 job 范围，不是文件或章节。本规格只借鉴“多文件按导入顺序连续导航、可跳到文件首段”的交互；协作 chunk 与权限由 `collaborative-job-chunks` 独立拥有。

## Scope

- **In**: `Project → Document → Segment` immutable contracts；三类 origin；稳定复合 ID；章节显示名、导入/manifest 顺序、章节切换与扁平导航适配；编辑/浏览模式的章节分隔；“当前章节 / 全部章节”搜索范围；可扩展搜索 scope；项目/文档 dirty 状态；source 更新 reconciliation；单文档与批量保存报告；目录多文件的 staging/失败恢复语义；workbook 单文件原子替换语义。
- **Out**: 重写当前单 JSON codec；具体 JSON/XLSX/RPY 语法解析与 round-trip writer；TM 存储/检索；speaker 显示 profile；把 TMX 打开为编辑项目；任意 Office workbook 支持；MateCat 式协作 job/chunk 分配与只读权限。

## Boundary Candidates

- `EditorProject` 只拥有项目级名称、语言、origin、文档集合和工作区状态；
- `ProjectDocument` 拥有稳定 `document_id`、`source_ref`、`display_name`、`order`、`codec_id`、写能力与段落集合；
- `ProjectSegment` 使用 `(document_id, local_segment_id)` 作为项目内稳定身份，章节名、sheet 名、行索引和列表位置不得充当持久 ID；
- 章节显示和导航只消费 `display_name/order`，重命名或重排不得改变文档与段落身份；
- 默认导航按导入/manifest 顺序把文档连续排列，但 UI 必须保留明确章节分隔、当前章节标识和“跳到章节首段”；
- 搜索范围首批使用 `current_document` 或 `entire_project`，UI 文案为“当前章节 / 搜索全部章节”；内部 scope 允许未来增加 `current_chunk`，但当前不得把 chunk 映射成 Document 或把未实现的 chunk 控件暴露给用户；
- 重新导入或应用源项目更新时，workspace 按稳定复合 ID 与 source fingerprint 报告 `unchanged`、`source_changed`、`new`、`removed`；`source_changed` 默认保留已有 target 但撤销确认，任何无法重关联的段落都必须显式交给用户处理；
- codec/parser 产生单文档内容和诊断，workspace 负责把多个文档组合成项目；
- codec-private token/sidecar 负责源格式保真，通用项目模型不解释格式 metadata。

## Out of Boundary

- 当前单 JSON 项目继续由既有路径打开和保存，不依赖本规格，也不因本规格阻塞 Parser Foundation；
- 多 JSON folder、multi-sheet XLSX 与 RPY 项目支持均后置，并显式依赖本规格与各自 codec；
- XLSX 只可由后续规格批准明确的工作表 profile，不因 `workbook` origin 自动支持任意表格；
- TMX 始终是 `language_resource` 互操作格式，不是 `project_document`，未来如需编辑应属于独立 TM Resource Editor；
- 不把 `confirmed`、speaker 显示名或头像强制写入不拥有这些字段的源格式。

## Upstream / Downstream

- **Upstream**: `parser-subsystem-extraction` 的中立 parsed records、purpose-aware codec registry、结构化诊断与 writer capability；现有 `EditorProject`/`EditorController` 行为基线。
- **Downstream**: 多 JSON 文件夹项目、multi-sheet XLSX 章节项目、`rpy-project-codec` 的单/多文档工作区接入、Qt 章节导航与保存反馈、`collaborative-job-chunks` 和 `cross-device-sync-plugin`。

## Existing Spec Touchpoints

- **Extends**: Parser rebaseline 中“单文档 codec 与跨文档 workspace 分离”的边界；Qt 编辑会话从扁平项目演进为多文档聚合。
- **Adjacent**: `rpy-project-codec` 拥有 RPY 语法/token/writer；未来 XLSX project codec 拥有 workbook profile 和只改目标列的 round-trip；`speaker-display-profiles` 只拥有显示覆盖；`tmx-context-interchange` 只拥有 TMX language resource 互操作。

## Constraints

- 文档身份必须来自 manifest 中的稳定 ID 或规范化相对 `source_ref`，不得依赖可截断/可重命名的 sheet/display name，也不得依赖枚举顺序。
- 段落复合 ID 在重新打开、章节重排和显示名变更后保持稳定；发生源结构变化时必须显式报告无法重关联的段落。
- 重新导入不得把 source 变化伪装成 target 文字预处理，也不得按当前列表索引猜测重关联。
- workbook 是单文件保存单元：完整生成、验证并以同目录临时文件原子替换；失败保留原文件和 dirty 状态。
- directory 是多文件保存单元，不能虚构跨文件 `os.replace` 原子性：先完成全部 staging/validation，再提交；失败报告必须逐文档区分 `saved`、`rolled_back`、`unchanged`、`failed`，保留未确认保存的 dirty 状态，并提供可重试/恢复信息。
- Reader/Writer 只修改各自明确拥有的字段；未知列、工作表、代码、注释、空白和格式内容的保留范围由格式专属规格用 golden fixture 验证。
