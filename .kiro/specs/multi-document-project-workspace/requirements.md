# 需求文档

## 简介

LocalCAT 当前将一个翻译项目表达为一个绝对路径和一组扁平段落。这个模型能服务现有单 JSON/TXT 工作流，却无法安全表达“一个项目包含多个章节文档”：章节显示名、排序和路径变化不应重写身份，一个文档的保存失败也不应被项目级“已保存”掩盖。

本规格引入稳定的 `Project → Document → Segment` 层级，并以版本化 ProjectPackage 作为首个可持久、可冷重开的 canonical 多文档 substrate。显式文件 intake 可以先 stage 多个 Document，但只有 ProjectPackage 保存 manifest、document members、编辑 overlay、摘要、preview/apply 与 receipt 后才形成 durable 项目；自动目录聚合、multi-sheet XLSX 与 RPY 产品入口后置。

## 范围边界

- **范围内**：Project/Document/Segment 不可变合同；项目、文档和复合段落身份；ProjectOrigin 叶合同与 ProjectPackage canonical persistence；用户显式选择多个 JSON/TXT/PO/POT 单文档输入并逐个经 Parser 验证的有界 intake；版本化 `ProjectPackageManifest`、document member、target/state overlay 和 opaque `codec_private_member`；member digest、路径安全和 stale binding；手工 export/validate/preview/import/apply/receipt；source reconciliation；文档/项目 dirty、保存报告与恢复；章节顺序、切换、连续导航、当前章节/全部章节搜索范围和 Qt 反馈；现有单 JSON 兼容适配。
- **范围外**：直接扫描多 JSON/TXT 目录；multi-sheet XLSX project profile；RPY codec 或 RPY 项目入口；PO/POT canonical/source-round-trip writer；TMX 项目文档；ResourcePackage 及 TM/术语资源导入导出；chunk 拆分/分配/权限；provider/远程列举/凭据/加密/同步冲突；TM CONTEXT、provenance/evidence 字段、Fuzzy 授权或 speaker display profile。
- **相邻期望**：Parser/Codec 只产生单文档内容、局部 ID、能力和诊断；workspace 聚合项目。`collaborative-job-chunks` 在 Cluster 2 通过后只引用稳定 segment identity。`cross-device-sync-plugin` 未来在项目侧只传输已批准 ProjectPackage、在资源侧只传输已批准 ResourcePackage，并分别复用各自的 import/apply 交易。现有 `language-resource-portability` brief 在本规格 Cluster 2 冻结后提升 JSONL/CSV ResourcePackage R/D/T；`tmx-context-interchange` 未来只增加可选 TMX export profile。

### Scope Lineage（范围沿革）

- 本文是 `multi-document-project-workspace` 的首份正式 Requirements；同目录 `brief.md` 继续作为需求来源，不再单独授权实施。
- 已完成的 `parser-subsystem-extraction` 只是上游单输入契约；本规格不回溯修改 Parser 的 purpose、grammar、terminal 或 writer capability。
- 经用户批准的范围收敛将首个真实多文档 substrate 从“直接 directory/workbook 聚合”调整为 ProjectPackage-first。`directory` 与 `workbook` 保留为后续 origin adapter/profile，不是本规格当前可验收输入。
- 新增 Cluster 0 只做治理、边界和可执行 inventory；原 Promotion Cluster 1–4 的编号和产品责任保持。
- ADR-018 只采纳项目 owner 已明确作出的 ProjectPackage/ResourcePackage 分权、`codec_private_member`、Chunk/Sync/RPY 顺序与 reader-only writer 边界；它不等于批准本文的详细 Requirements/Design/Tasks。`spec.json` 在 Cluster 0 人工门前继续保持三阶段 `approved=false`。
- Cluster 2 同时冻结聚合/持久化/source reconciliation 和手工 ProjectPackage 闭环。这是 Chunk 开始实施的硬门；只完成 Cluster 1 身份类型不足以开始 Chunk。
- ProjectPackage 与后续 ResourcePackage 是两个正交的持久/交换单元：前者拥有项目内容和编辑状态，后者拥有 TM/术语资源可移植产物。本规格不建立两者的共同 authority。
- RPY 产品实施排在 Sync 主线之后；TMX 仍只是 language resource；PO/POT writer 需等待后续 codec 规格批准。这些后置项不得以预留产品控件或格式专属字段的方式进入本规格。

## 术语

- **Project**：一个翻译工作区的稳定身份、语言、origin、有序 documents 和项目状态。
- **Document**：项目内一个内容/章节单元，拥有稳定 `document_id`、规范化 `source_ref`、显示信息、codec 能力和有序 segments。
- **Segment_Identity**：项目内稳定复合键 `(document_id, local_segment_id)`。
- **ProjectOrigin**：项目外部 source origin 的不可变投影；叶类型为 `single_file`、`directory` 与 `workbook`。叶合同本身不授权目录发现、XLSX profile 或 source write-back。
- **ProjectPersistence**：工作区的 canonical 持久面；现有单 JSON 可继续使用 legacy compatibility persistence，首个真实多文档 persistence 为 ProjectPackage。
- **ProjectPackage**：由版本化 manifest、项目/document members、摘要和编辑 overlay 组成的项目导入导出单元。
- **ProjectPackageManifest**：以稳定 ID、member 引用、摘要、顺序和能力描述完整项目的版本化逻辑清单；物理容器形态不是身份。
- **codec_private_member**：由格式 codec 拥有的可选 opaque member 引用；workspace、chunk、sync provider 与 Qt 均不解释内容。
- **Editing_Overlay**：ProjectPackage 拥有的 target、确认/需复核与其他获批准编辑状态；对 reader-only source，overlay 是唯一可持久编辑面。
- **Reconciliation**：用稳定复合身份和 source fingerprint 对新旧 source 状态分类，产生显式 preview，再由用户决定如何 apply。

## 需求

### Requirement 1：Promotion Cluster 与实施授权

**目标：** 作为项目 owner，我希望多文档以人工门分簇推进，以便治理、身份、持久化和 UI 不会互相抢跑。

#### 验收标准

1. The Multi-Document 推进顺序 shall 保持 Cluster 0 → 1 → 2 → 3 → 4，且每一簇都需要人工批准后才能进入下一簇。
2. While Cluster 0 尚未通过, the implementation shall 只变更 Requirements/Design/Tasks、需求边界、已采纳 ADR 的一致性说明、现状 inventory、Cluster 0 characterization/architecture tests 与测试计划，不得修改 production 合同、schema、Controller、Qt 或 owner evidence payload。
3. When Cluster 1 实施时, the implementation shall 只交付 Project/Document/Segment、ProjectOrigin、复合 identity 与现有单 JSON 兼容适配。
4. When Cluster 2 实施时, the implementation shall 交付 ProjectPackage 聚合/持久化、source reconciliation、手工导出导入和失败恢复闭环。
5. While Cluster 2 尚未通过, the `collaborative-job-chunks` implementation shall 不得开始，也不得用 Document 替代 chunk。
6. When Cluster 3 实施时, the implementation shall 交付 Qt 无关的 Controller session、dirty/save/search scope 与 issued identity，不得让 Controller 解析 codec-private 语法。
7. When Cluster 4 实施时, the implementation shall 交付 Qt 章节导航、保存/恢复反馈与真实 ProjectPackage 冷重开验收，不得以扁平单 JSON fixture 替代多文档验收。

### Requirement 2：稳定 Project/Document/Segment 身份

**目标：** 作为译者，我希望章节改名、重排或重开后仍定位到同一文档和段落，以便 target、进度和后续 chunk 引用不漂移。

#### 验收标准

1. When 创建或导入项目时, the workspace shall 为 Project 持有一个非空、稳定且包内唯一的 `project_id`。
2. When 文档进入项目时, the workspace shall 为每个 Document 持有稳定、项目内唯一的 `document_id`，并保留其 `codec_id`、能力快照、规范化 `source_ref`、`display_name` 与 `order`。
3. The 项目内 segment identity shall 严格为 `(document_id, local_segment_id)`，且 `local_segment_id` 必须在所属 Document 内唯一。
4. When 用户修改 `display_name` 或文档 `order`、移动整个包的绝对位置、或切换 UI 排序时, the workspace shall 保持 `project_id`、`document_id` 和 Segment_Identity 不变。
5. The workspace shall 不得将显示名、sheet 名、文件 stem、枚举顺序或扁平列表下标作为持久身份。
6. If manifest 中的 project/document/segment 身份为空、重复、形状不合法或引用不存在的所属对象, the workspace shall 在发布任何项目状态前 fail closed。
7. When 现有单 JSON 满足 Workspace v1 eligibility 并通过兼容适配打开时, the workspace shall 保留当前 segment id、顺序、target、speaker 和 confirmed 行为；if 其 name/local ID/source ref 不满足 v1 安全限制, the adapter shall 结构化拒绝提升、保持原文件不变，且不影响旧 `load_project()` / `save_project()` 继续使用。

### Requirement 3：ProjectOrigin 与首个真实多文档 substrate

**目标：** 作为产品 owner，我希望先用一个可完整验证的 ProjectPackage 打通多文档，以便不把目录扫描或 workbook 假设偷渡成身份契约。

#### 验收标准

1. The 现有单文档兼容适配 shall 投影 `single_file` ProjectOrigin，且首个 durable/reopenable canonical 多文档 persistence 与 import substrate shall 是 ProjectPackage，不得将 ProjectPackage 伪造为第四种 source origin；显式文件列表只产生未发布的 creation/staging candidate。
2. When ProjectPackage 成功导入时, the workspace shall 从 manifest 和 verified members 建立两个或以上 Documents，不得从当前文件系统枚举顺序反推文档身份。
3. While 直接 directory origin 的后续 profile 尚未批准, the product shall 不得扫描目录并自动创建多 JSON/TXT 项目。
4. While multi-sheet XLSX project profile 尚未批准, the product shall 不得将术语 XLSX codec 的 active worksheet 能力提升为 workbook ProjectOrigin，也不得将任意 worksheet 当作 Document。
5. While RPY 实施尚未在 Sync 主线后获得独立批准, the product shall 不得打开、聚合或写回 RPY 项目，也不得在通用合同中预埋 RPY 语法。
6. If 包声明未知或未批准的 ProjectOrigin/profile, the workspace shall 返回结构化 unsupported 失败并保持当前项目不变。
7. When 用户首次创建多文档项目时, the product shall 只接受用户显式选择且位于一个已验证 portable root 下的 JSON、TXT、PO 或 POT 文件列表，逐个通过既有 `project_document` Parser surface 获得 verified terminal 后聚合，并将完整 workspace 保存为 ProjectPackage。
8. The 显式文件 intake shall 在同一 retained rooted binding 下保留用户选择顺序，拒绝重复/hardlink alias/越界/非 regular/symlink/root 或文件 drift 输入并设置 `directory/explicit-selected-files-v1` origin profile；未选择文件不得被 Core 枚举、读取、自动吸收或影响 binding，也不得据此授予 source writer。
9. While `directory/explicit-selected-files-v1` 是当前 origin profile, the workspace shall 只允许保存 ProjectPackage，不得对所选 JSON/TXT/PO/POT 执行多文件 origin write-back；既有单文件 LocalCAT JSON writer 仅保留在 legacy `single_file` compatibility path。

### Requirement 4：版本化 ProjectPackageManifest 与 member 边界

**目标：** 作为需要手工备份和迁移项目的译者，我希望包内的每个成员都有可验证引用，以便损坏、丢失和替换不会被静默忽略。

#### 验收标准

1. The ProjectPackageManifest shall 声明受支持的 manifest schema version、`project_id`、项目语言、有序 document entries 和每个受管 member 的类型、规范化相对引用、字节数与密码学摘要。
2. When 读取 document entry 时, the workspace shall 要求稳定 `document_id`、`source_ref`、`display_name`、`order` 和受摘要绑定的 document member；该 member shall 完整保存 format/codec identity、source binding、segments 与 ProjectPackage 拥有的 Editing_Overlay 状态，且持久信息不得自行授权 live writer。
3. Where 一个 codec 需要格式私有保真材料, the manifest shall 只使用精确名称 `codec_private_member` 持有其 member 引用和摘要，不得将内容投影为通用字段。
4. The workspace、Controller、Qt、chunk 和 sync provider shall 不得解码、改写或从 `codec_private_member` 推断业务语义。
5. If manifest 版本不受支持、必需 member 缺失、member 大小/摘要不匹配、未声明 member 影响规范语义，或同一 member 被冲突引用, the validator shall fail closed 且不产生可 apply preview。
6. The ProjectPackageManifest shall 不得收编 live canonical TM SQLite、sidecar、journal、stage residue、device qualification 或凭据。
7. The ProjectPackage contract shall 不得声明自己是 ResourcePackage，也不得把 TMX、TM JSONL 或术语 CSV/v1 资源成员提升为 Project Document authority。
8. While exact matching live codec 不可用, the package shall 仍允许离线打开、导入、target编辑与ProjectPackage保存，并只投影body-safe `PROJECT.PACKAGE.CODEC_UNAVAILABLE` warning；source write-back与`codec_private_member`读取shall fail closed，且availability不得铸造writer authority。

### Requirement 5：路径安全与包内引用

**目标：** 作为导入外部项目包的用户，我希望所有路径都被限制在包边界中，以便 preview 不会因为路径欺骗读取或覆盖其他文件。

#### 验收标准

1. The `source_ref` shall 使用 origin 内规范化的可移植相对引用，所有 member reference shall 使用 ProjectPackage 内规范化的相对引用；两个命名空间都必须执行唯一性与冲突检查。
2. If 一个引用为绝对路径、包含 NUL 或 `..` 逸出、指向 symlink/非 regular member、穿越包根，或在规范化后与另一引用冲突, the validator shall 在读取正文或创建 target 前拒绝包。
3. When 包的外部绝对位置变化时, the workspace shall 使用 manifest-issued identity 重开同一项目，不得因新父目录重新铸造 ID。
4. When 导出到用户选择的 target 时, the exporter shall 只在经验证的 target parent 内构建独占 candidate，且不得跟随中途替换的 parent/member 身份。
5. If target parent/member identity 在发布前改变或最终 readback 不一致, the exporter shall 拒绝发布并保留已有 target 不变。

### Requirement 6：Reader-only source 的 target 与编辑状态

**目标：** 作为编辑 TXT、PO 或 POT 等 reader-only 文档的译者，我希望自己的译文和复核状态在项目里安全保留，同时不要把它们冒充为对源格式的无损回写。

#### 验收标准

1. When Document 的 codec capability 不声明 writer 时, the ProjectPackage shall 在 Editing_Overlay 中拥有该 Document 的 target、confirmed/需复核状态和 dirty baseline。
2. When 用户修改 reader-only Document 的 target 或确认状态时, the workspace shall 只更新内存工作区与后续 package-owned overlay，不得修改原 source member 或外部源文件字节。
3. When 导出并冷重开包含 reader-only Documents 的 ProjectPackage 时, the workspace shall 恢复每个 Segment_Identity 对应的 target 和编辑状态，同时证明 source member 字节未被改写。
4. If overlay 引用不存在的 Segment_Identity、对同一段落重复赋值，或与绑定的 source fingerprint 不一致, the importer shall 在产生可 apply preview 前 fail closed。
5. While PO/POT writer 未由后续 codec 规格批准, the workspace shall 不得声明或提供 PO/POT source write-back；TXT 亦 shall 保持 reader-only。
6. The Editing_Overlay shall 不得存储 TM CONTEXT、retrieval evidence、speaker alias/avatar 或 chunk permission。

### Requirement 7：Source reconciliation 与无法重关联的处理

**目标：** 作为收到新版源文档的译者，我希望看到哪些段落未变、已变、新增或移除，以便旧 target 不会被按下标猜测后错配。

#### 验收标准

1. When 重新导入或应用 source 更新时, the workspace shall 用稳定 `document_id`、`local_segment_id` 和 source fingerprint 分类 `unchanged`、`source_changed`、`new`、`removed`、`ambiguous` 与 `unresolved`。
2. When 段落为 `unchanged` 时, the reconciliation preview shall 保留其 target 和已有确认状态。
3. When 段落为 `source_changed` 时, the reconciliation preview shall 默认保留旧 target 作为待复核草稿，且撤销确认状态。
4. When 段落为 `new` 时, the reconciliation preview shall 为它建立新复合身份和不伪造确认的初始 overlay。
5. When 段落为 `removed`、`ambiguous` 或 `unresolved` 时, the workspace shall 保留旧 overlay 与可恢复引用，并要求用户显式处理，不得静默删除或自动贴到其他段落。
6. The reconciliation algorithm shall 不得仅根据当前列表下标、文本相似、display name 或文件枚举顺序猜测身份。
7. If 存在任何 `ambiguous`/`unresolved` 且用户未做完显式决定, the workspace shall 拒绝将 preview 发布为新工作区权威。
8. When 对显式选择的外部 source 重新执行 reconciliation 时, the Application shall 只在同一已验证 root binding、规范化 `source_ref` 与既有 binding revision 均匹配时复用 manifest `document_id`；新的 sealed source identity 可以变化并作为 incoming source 事实参与 fingerprint/reconciliation，preview 后再次变化才判 stale。重命名必须由用户显式确认旧新引用映射，forged/stale binding 不得回接身份。

### Requirement 8：手工 ProjectPackage 导出、验证、预览、导入与 receipt

**目标：** 作为需要人工备份或转移项目的译者，我希望在覆盖当前工作前看到完整预览，并在成功后得到可重开核对的 receipt。

#### 验收标准

1. When 用户导出 ProjectPackage 时, the exporter shall 在新 candidate 中完整生成 manifest、所有成员和摘要，再独立验证 candidate。
2. If 导出的任何成员生成、fsync、摘要复读、manifest closure 或最终发布失败, the exporter shall 不得覆盖已有成功包，并返回结构化失败/恢复信息。
3. When 用户选择待导入包时, the importer shall 先对完整 manifest、路径、member identity/digest、codec capability、overlay 和版本执行只读 validation。
4. When validation 成功时, the importer shall 产生一个不修改当前 workspace 的 preview，至少说明项目身份、文档/段落数、编辑状态数、reconciliation 类别、warning 和阻断原因。
5. The import preview shall 绑定输入包身份、manifest/member digests、codec versions 和当前 workspace revision。
6. If 输入包或当前 workspace 在 preview 后发生任何绑定变化, the importer shall 将 preview 判为 stale，拒绝 apply 并保持当前 workspace 不变。
7. When 用户显式批准一个仍有效的 preview 时, the importer shall 在同一 import/apply 交易中完成全成员 stage、终态验证与 workspace 发布。
8. If import/apply 中任何文档、member、overlay、reconciliation 或发布步骤失败, the importer shall 保留 last-known-good workspace、当前 dirty 与源字节，不得发布部分新项目。
9. When import/apply 完整成功时, the importer shall 返回结构化 receipt，绑定操作 ID、包/manifest digest、新 workspace revision、处理数量、warning 和发布结果。
10. When 从 receipt 指向的已发布包冷重开时, the workspace shall 恢复同一 Project/Document/Segment identity、顺序、target/state overlay 和 dirty baseline。

### Requirement 9：Dirty、保存报告与恢复状态

**目标：** 作为同时编辑多个章节的译者，我希望知道哪个文档尚未安全保存，以便一个失败不会清除其他未完成的 dirty 状态。

#### 验收标准

1. When 一个 segment target 或获批准的编辑状态变化时, the workspace shall 将所属 Document 标记为 dirty，并由所有 document dirty 状态派生 project dirty。
2. When 只保存一个 Document 时, the save operation shall 只清除终态验证且发布成功的该 Document dirty，不得清除其他 Documents 的 dirty。
3. When 执行项目级保存或导出时, the save service shall 返回结构化项目/文档报告，明确标记 `saved`、`unchanged`、`failed`、`rolled_back` 和需恢复状态。
4. If 一个或多个文档无法安全发布, the save service shall 保留所有未被证明已保存的 dirty，并为每个受影响文档返回可重试/恢复信息。
5. If 故障发生在最终发布边界且无法证明新旧状态, the save service shall 将结果标记为需恢复/不确定，不得报告成功或清除 dirty。
6. When 下一次冷启动发现未完成发布记录或 candidate residue 时, the recovery service shall 先证明 last-known-good/candidate 身份与摘要，再完成、回滚或要求人工处理。
7. While recovery 结果不确定, the workspace shall 不得静默覆盖包、原 source 或编辑 overlay。

### Requirement 10：Controller 导航、进度与搜索范围

**目标：** 作为翻译多章节项目的译者，我希望在保留章节边界的同时连续导航、查看进度和搜索。

#### 验收标准

1. When 打开多文档项目时, the Controller shall 按 manifest/document `order` 建立稳定导航投影，并保留每个 Segment_Identity 而非只暴露扁平下标。
2. When 用户从一个 Document 的末段向后导航时, the Controller shall 进入下一个有序 Document 的首段，并更新当前章节投影。
3. When 用户选择一个章节时, the Controller shall 跳转到其首个可导航段落，不得改变文档/段落身份或丢失未保存 target。
4. The Controller shall 分别计算当前 Document 进度和整个 Project 进度，且不得把 Document 进度命名为 chunk 进度。
5. When 用户选择 `current_document` 搜索范围时, the search service shall 只搜索当前 Document 并用 Segment_Identity 返回命中。
6. When 用户选择 `entire_project` 搜索范围时, the search service shall 按项目导航顺序搜索所有 Documents，UI 文案 shall 为“搜索全部章节”。
7. While Chunk 规格尚未交付, the internal search scope shall 只保留未来 `current_chunk` 的可扩展性，不得向用户暴露 chunk 控件或将当前 Document 映射为 chunk。
8. If 导航或搜索命中携带的 project/document/segment revision 与当前 workspace 不一致, the Controller shall 拒绝该 stale 操作且不改变当前 target、dirty 或位置。

### Requirement 11：Qt 章节体验与可访问失败反馈

**目标：** 作为桌面端译者，我希望始终知道自己正在哪个章节，并能看到导入、保存或恢复的真实结果。

#### 验收标准

1. When 多文档项目可见时, the Qt 编辑器 shall 在原顶栏布局中只新增无下拉箭头的文件夹入口，用文件图标+display name、无数字序号前缀的菜单显示当前文档、顺序和可键盘操作的切换/跳转入口。
2. Where 连续段落列表跨越 Document 边界, the Qt 编辑器 shall 用文件图标+display name 显示明确文档分隔和当前文档状态，不得添加“章节 N”干扰文案或把所有段落显示为无边界的单文件。
3. When 切换 Edit/Browse 模式、宽窄布局或章节时, the Qt 编辑器 shall 保持同一 Controller workspace 与 Segment_Identity，不得复制或覆盖未保存 target。
4. When 用户在“项目”菜单选择 ProjectPackage 导入且 preview 可用时, the Qt 编辑器 shall 用 LocalCAT 自有对话框显示 `NEW`/`REPLACE`/`UPDATE` 模式、当前项目→incoming 项目、单行可复制完整 project ID、文档/段落数、reconciliation 类别、warning 和阻断原因，并要求用户显式批准 apply，默认焦点/动作必须为取消；该能力不得占用文件夹导航入口或另建常驻顶栏。Where 当前会话仍是 Legacy 项目, Qt shall 先要求选择新的 ProjectPackage destination，preview 前后保持 Legacy 会话与原文件不变，apply 成功后才发布 incoming 包并切换为 workspace；该跨项目 import 是 `NEW`/`REPLACE`，不得伪装成把 Legacy 内容合并或提升进 incoming 包。
5. If 导入、保存或恢复失败, the Qt 编辑器 shall 指明受影响的文档、安全原因、dirty 保留与可重试/恢复状态，不得仅显示“保存完成”或吞掉失败。
6. When 导出或导入完整成功时, the Qt 编辑器 shall 提供不含 source/target 正文的结构化数量摘要和 receipt 状态。
7. The Qt 编辑器 shall 只消费 Controller 的 frozen projection，不得直接读写 manifest、member、Parser codec、source snapshot 或 recovery journal。
8. When 浏览/校对的当前 segment 行变化时, the Qt 编辑器 shall 同步 Controller current Segment_Identity、当前文档标题与文件夹菜单选中态，不得保留上一文档的投影。
9. When 用户通过首页、项目主按钮、项目菜单或 `Ctrl+O` 打开或拖入本地文件时, the Qt 编辑器 shall 使用同一入口按单选/多选分流；单选直接打开，Shift 多选进入有序显式选择 review，不得显示独立“单文档/多文档”控件、独立菜单项或可见 drop-zone 控件。

### Requirement 12：兼容性、本地性与边界验收

**目标：** 作为现有 LocalCAT 用户和规格审批者，我希望新工作区不回归单文档流程，也不把未批准的相邻功能当成完成。

#### 验收标准

1. The implementation shall 保持现有单 JSON 打开/保存、TXT 打开、segment id、speaker、target、confirmed/dirty、搜索、TM/术语建议与 Excel 三态行为。
2. When 验收多文档身份、导航、dirty、导入导出或冷重开时, the acceptance shall 使用至少含两个 Documents 且跨文档存在相同 local segment id 的真实 ProjectPackage，不得只测试扁平内存 fixture。
3. When 验收 reader-only 文档时, the acceptance shall 编辑 target、导出、冷重开并核对 source 字节不变；仅看到 UI target 更新不算通过。
4. When 验收 stale 和失败语义时, the acceptance shall 覆盖 manifest/member 篡改、路径逸出/冲突、preview 后包变化、preview 后 workspace 变化、部分 stage 失败、发布故障与冷启动恢复。
5. If 任一阻断故障发生, the acceptance shall 证明 last-known-good workspace/包、原 source、当前 target、dirty 与导航位置符合各自的零非授权修改语义。
6. The implementation shall 在本机完成项目解析、package 处理、reconciliation、导航与保存，不得向网络发送项目、资源或诊断。
7. The Project/Document/Segment 与 ProjectPackage shall 不得增加 TM CONTEXT/evidence、TMX export profile、ResourcePackage authority、chunk permission、provider 凭据、RPY token 或 speaker display 字段。
8. When 输入 TMX 或将 TMX 声明为 Project Document 时, the registry/workspace shall 在创建 Project/Document identity 前拒绝该用途组合。

## 非功能约束

- 所有跨层契约使用不可变值、tuple 集合和结构化 report；任何 preview/receipt 不嵌入 source/target 正文。
- Parser/Codec 与 workspace 保持互不反向导入；Qt 只依赖 Controller frozen contracts。
- ProjectPackage 的逻辑 manifest/member/receipt 语义先于物理容器选择；任何物理适配都必须保持同一身份、digest、stale、事务与恢复语义。
- 所有未知版本、未知能力、身份冲突、路径逸出、摘要不一致、stale preview 和发布不确定均 fail closed。
