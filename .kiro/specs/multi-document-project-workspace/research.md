# 研究与设计决策

## Summary

- **Feature**：`multi-document-project-workspace`
- **Discovery Scope**：现有单文档 Qt/Application 与已重新基线的 Parser 之间的复杂扩展
- **Key Findings**：
  - 当前 `EditorProject` 只有一个可选绝对 `path` 和一个项目内唯一的扁平 `segments`；Controller 只持有一个 `current_index` 和项目级 dirty 状态，不能表达文档身份、文档级失败或跨文档调和。
  - Parser 已为 `project_document` 提供 LocalCAT JSON、TXT、PO 和 POT 的单输入读取，但只有 LocalCAT JSON 声明 canonical writer；TXT/PO/POT 的后续 target 与编辑状态需由 ProjectPackage 保存，不能伪装成源格式回写。
  - TMX 在 purpose-aware registry 中只属于 `language_resource.translation_memory`；XLSX 当前只是术语资源的 active-worksheet reader，两者都不能被多文档工作区推断成项目文档。
  - 首个可验收的真实多文档 substrate 应是版本化 ProjectPackage：它同时给出稳定身份、成员摘要、编辑 overlay、preview/apply/receipt 和冷重开边界。直接目录聚合、multi-sheet XLSX 与 RPY 均不是本轮 substrate。
  - ProjectPackage 是项目工作区的交换/恢复单元；后续 JSONL/CSV ResourcePackage 由 `language-resource-portability` 独立拥有，`tmx-context-interchange` 未来只增加可选 TMX export profile。二者不共享 authority，也不应抽象为一个“通用包”。

## Research Log

### 当前 Editor 项目模型

- **Sources Consulted**：`editor_contracts.py`、`editor_project.py`、`editor_controller.py`、`project_search.py`及对应 tests。
- **Findings**：
  - `EditorProject(name, segments, source_locale, target_locale, path)` 要求 segment id 在整个扁平项目中唯一，没有 document id、origin、member 或 document-local dirty。
  - `editor_project.load_project()` 的产品入口仅根据 `.json` / `.txt` 选择 codec；`save_project()` 只向 `.json` 执行 LocalCAT canonical write。
  - Controller 以扁平索引导航、计算确认进度和绑定搜索/TM 建议；当前 dirty 是一个项目级 bool，保存成功后整体清除。
- **Implications**：多文档必须先引入不受显示名、顺序与扁平位置影响的 Project/Document/Segment 合同，再改造 Controller/Qt；不能只在现有 segment 上附加一个可变 chapter label。

### Parser/Codec 能力与 writer 边界

- **Sources Consulted**：`parser_contracts.py`、`parser_composition.py`、`parser_localcat_codec.py`、`parser_gettext_codec.py`、`parser_tmx_codec.py`、`parser_termbase_codec.py`、ADR-015 与 Parser Requirements/Design。
- **Findings**：
  - Parser 只产生单输入 `DocumentHeader` / `ParsedSegment` / terminal，局部 ID 只在单文档内有效。
  - LocalCAT JSON 具有 canonical write；TXT、PO、POT 均为 reader-only。PO/POT 保留 gettext metadata，plural 在当前 singular profile 下 fail closed。
  - Source boundary 已有 rooted/sealed snapshot identity、内容摘要和 verified terminal，可作为 ProjectPackage member 和 source reconciliation 的上游事实，但 Parser 不拥有项目级 ID、dirty、package 或交易。
  - `RoundTripTokenEnvelope.opaque_payload` 是 codec-private 能力，通用 workspace 不应解析或重新命名其语义。
- **Implications**：多文档聚合只消费 Parser 终态和能力快照。通用 manifest 中对格式私有保真成员的唯一名称为 `codec_private_member`，它只是受摘要绑定的 opaque member 引用。

### 多文档的首个真实 substrate

- **Context**：旧 brief 曾同时列出 `single_file` / `directory` / `workbook` origin，但直接扫描目录或解释 workbook 会在身份、顺序和失败语义未冻结时引入格式特例。
- **Alternatives Considered**：目录优先；multi-sheet XLSX 优先；ProjectPackage 优先。
- **Selected Approach**：以逻辑 ProjectPackage 作为首个 durable/reopenable canonical 多文档 persistence/import substrate。显式文件 intake 只建立 staged candidate；Cluster 1 冻结身份/origin 和单 JSON 兼容适配，Cluster 2 交付版本化 manifest、聚合、调和、手工 export/validate/preview/import/apply/receipt 及恢复闭环。
- **Rationale**：包内 manifest 可在不依赖外部路径枚举顺序的前提下同时冻结 project/document/segment 身份、member digest 和编辑 overlay。
- **Deferred**：直接 directory origin、multi-sheet XLSX project profile、RPY codec/project 入口。这些未来 adapter 只能生成或消费同一 workspace/package 合同，不得另造身份体系。

### 第一个 ProjectPackage 的创建入口

- **Problem**：如果只允许导入已有 ProjectPackage，而目录扫描、workbook 与 RPY 又全部后置，产品就没有办法创建第一个真实多文档包。
- **Selected Approach**：首批提供显式文件 intake。用户先选择一个受验证的 portable root，再显式选择其中的 JSON/TXT/PO/POT 文件；Application 对每个文件调用既有 `project_document` Parser surface，只有全部输入取得 verified terminal 后才聚合 workspace 并保存 ProjectPackage。
- **Boundary**：该入口使用 `directory/explicit-selected-files-v1` 描述历史 source origin，但不递归扫描目录、不自动包含相邻文件、不解释 workbook/sheet，也不改变 codec writer capability。PO/POT 因而可作为 reader-only ProjectDocument 使用；target/状态只进入 package overlay。
- **Rationale**：这使真实 exporter/importer 有产品可达的多文档输入，同时不把“显式选择一组已知文件”扩张成后续的 folder discovery profile。

### ProjectPackage 与 ResourcePackage

- **ProjectPackage 唯一职责**：项目、文档、段落的稳定身份；source snapshot/member；target/确认/编辑 overlay；项目级进度；手工导入导出、preview/apply/receipt 和 source reconciliation。
- **ResourcePackage 唯一职责**：由 `language-resource-portability` 冻结的 TM JSONL/术语 CSV/v1 资源 profile、导入导出产物、报告和 receipt；它不是项目文档容器。
- **tmx-context-interchange 唯一职责**：未来可增加的 TMX export profile、context/provenance 与有损互操作语义；它不拥有 ResourcePackage container/apply authority。
- **Decision**：不建立共同 package authority。ProjectPackage manifest 不收编 live canonical SQLite、sidecar、journal、stage residue 或 TMX 项目文档；sync 未来分别消费 Core 批准的项目包和资源包。
- **Follow-up**：在 Multi-Document Cluster 2 手工项目包闭环冻结后，恢复/确认 `language-resource-portability` brief 并推进 JSONL/CSV ResourcePackage 收尾；TMX profile继续由 `tmx-context-interchange` 后置治理。两者都不回写本 Spec 成为统一 authority。

### Reader-only 源的编辑所有权

- **Problem**：TXT 只有 source，PO/POT 可读但没有已批准 writer。若 UI 允许 target 编辑却只将状态放在内存，重开就会丢失；若直接写回源，则伪造了 codec 能力。
- **Selected Approach**：ProjectPackage 保存 reader-only document 的 target、confirmed/需复核状态和 dirty baseline，并以 source fingerprint 绑定。导出/保存项目只更新 package-owned overlay，不修改原 TXT/PO/POT 字节。
- **Failure Semantics**：源变化后必须进入 reconciliation；`source_changed` 可保留 target 作为待复核草稿，但撤销确认。不能按列表下标或相似文本静默重关联。

### 身份、路径、stale 与恢复

- **Identity**：`project_id` 与 `document_id` 由 manifest 持久化；segment 使用 `(document_id, local_segment_id)`。`display_name`、`order`、sheet 名、列表位置和包的绝对位置都不是身份。
- **Path**：`source_ref` 是 origin 内的可移植相对引用，member reference 是 ProjectPackage 内的相对引用；两个命名空间都必须规范化，且对绝对路径、`..`、NUL、特殊文件、symlink 逸出和规范化后冲突在 preview/apply 前 fail closed。
- **Stale**：preview 必须绑定导入包身份、manifest/member digests 和当前 workspace revision；任一变化都拒绝 apply。
- **Recovery**：导出先构建、校验完整 candidate，再发布；导入先 stage 全部成员，同一 import/apply 交易成功后才更换 workspace。故障保留 last-known-good 与可操作恢复信息，不能以“大部分文档可读”发布部分项目。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Decision |
|---|---|---|---|---|
| 扩展扁平 `EditorProject.segments` | 为段落添加 chapter label | diff 小 | 显示字段成为身份，无法表达文档级失败 | Reject |
| Directory-first | 直接扫描目录并按枚举顺序组项目 | 用户输入直观 | 路径、重命名、排序和部分保存过早耦合 | Defer |
| Workbook-first | 把每个 sheet 直接当 Document | 接近已知样本 | 会把术语 XLSX profile 与项目 workbook profile 混同 | Defer |
| ProjectPackage-first | 先冻结逻辑 manifest/member/overlay/receipt | 可独立验收身份、恢复和冷重开 | 需在 Design 中严格区分逻辑包与未决定的物理容器 | Select |
| 通用 Package authority | ProjectPackage 与 ResourcePackage 复用同一 schema/owner | 表面上少一层 | 项目身份与资源 authority 互相污染 | Reject |
| 直写 reader-only source | workspace 自行生成 TXT/PO/POT | 似乎方便 | 越过 codec capability，无 round-trip 保证 | Reject |

## Design Decisions

### Decision：ProjectPackage-first，但不提前铁定物理容器

- **Selected Approach**：Requirements 冻结版本化 logical manifest、members、digests、preview/apply/receipt 和发布不变量；Design 可选择最小物理适配，但不得让物理容器路径成为项目身份。
- **Rationale**：这个 seam 同时服务手工备份/迁移和后续 S3/WebDAV 传输，但后续 provider 仍只传输同一包，不拥有 import/apply 语义。

### Decision：包内编辑 overlay 是 reader-only document 的唯一可持久 target 面

- **Selected Approach**：每个 reader-only document 的 source member 与 target/state overlay 分开绑定；保存、导出与冷重开只更新/验证 package-owned overlay。
- **Rationale**：既保留 TXT/PO/POT 源字节，又让用户的 target 和确认状态可恢复。

### Decision：Chunk 门在 Cluster 2 之后

- **Selected Approach**：Cluster 0 只完成治理；Cluster 1 冻结稳定身份；Cluster 2 同时冻结 reconciliation、持久化和手工包闭环。只有 Cluster 2 验收通过后，`collaborative-job-chunks` 才可引用稳定 segment identity。
- **Rationale**：Chunk 不仅依赖 ID 类型，还隐式依赖 source update 后成员引用如何保留、撤销或进入冲突。

### Decision：后续线保持正交

- 直接目录/multi-sheet XLSX 属于未来 project origin/codec profile，不是本次 ProjectPackage 验收的替代品。
- RPY 产品实施排在 Sync 主线之后；本 Spec 不预埋 RPY token、speaker 或 writer 字段。
- PO/POT canonical/source-round-trip writer 由后续 codec Spec 批准；本 Spec 只保存 package overlay。
- TMX 仍是 language resource interchange；TM CONTEXT 投影、provenance/evidence 字段和 TMX export profile 不进入 Project/Document/Segment 或 ProjectPackage。

## Promotion Clusters 与人工门

| Cluster | 交付内容 | 不允许抢跑 |
|---|---|---|
| 0 | Requirements/Design/Tasks、border/已采纳 ADR-018 一致性复核、现状与迁移 inventory、characterization/architecture tests | 任何 production 合同、schema、Controller/Qt 或 owner evidence payload 改动 |
| 1 | Project/Document/Segment、ProjectOrigin、复合 identity、单 JSON 兼容 adapter | 目录扫描、workbook/RPY、chunk |
| 2 | ProjectPackage manifest/member/overlay、aggregation、reconciliation、save/recovery 与手工 export/validate/preview/import/apply/receipt | provider、ResourcePackage、chunk 语义 |
| 3 | Controller session、document/project dirty、issued identity、current_document/entire_project scope | Qt 自行解析 manifest 或 codec member |
| 4 | Qt 章节导航、连续段落体验、保存/恢复反馈与 current-source acceptance | 未批准的目录/XLSX/RPY 产品入口 |

每个 Cluster 都等待人工批准后才进入下一簇。Cluster 2 通过是 Chunk 的硬前置；RPY 实施继续排在 Sync 之后。

## Risks & Mitigations

- **包路径成为身份**：以 manifest-issued ID 为权威，对相对 member 引用做规范化与冲突检查。
- **reader-only 编辑丢失**：target/state 进 package overlay，冷重开验证；源字节不变。
- **partial import 发布**：全成员 stage + validate，同一 import/apply 交易只发布 verified terminal。
- **stale preview 覆盖当前工作**：preview 绑定包摘要与 workspace revision，apply 前重验。
- **无法重关联时静默丢 target**：报告 `removed`/`ambiguous`/`unresolved`，保留可恢复 overlay，由用户显式裁决。
- **未来格式污染当前合同**：仅保留 `codec_id`/capability 和 opaque `codec_private_member`；不增加 sheet/RPY/TMX/CONTEXT 专属字段。

## References

- `.kiro/specs/multi-document-project-workspace/brief.md`
- `.kiro/specs/parser-subsystem-extraction/{requirements.md,design.md,research.md}`
- `.kiro/specs/collaborative-job-chunks/brief.md`
- `.kiro/specs/cross-device-sync-plugin/brief.md`
- `.kiro/specs/rpy-project-codec/brief.md`
- `.kiro/specs/tmx-context-interchange/brief.md`
- `.kiro/steering/{product.md,roadmap.md,structure.md,tech.md,spec-ownership.md}`
- `.kiro/steering/adr/adr-015.md`
- `editor_contracts.py`, `editor_project.py`, `editor_controller.py`
- `parser_contracts.py`, `parser_composition.py`, `parser_*_codec.py`
