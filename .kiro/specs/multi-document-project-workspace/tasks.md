# 实施计划

## 任务说明

> **WA-03 Windows compatibility amendment（current R2）**：以下 `a` 后缀任务把 Workspace/ProjectPackage 的 root、lock 与 publication 接到 ADR-020及ADR-025的`WindowsDocumentedPublishV1`；ADR-018/019 的 logical/physical carrier authority 不变，`R1`只保留在dispatch历史。

本计划把 brief 的四个 Promotion Cluster 保持为 Cluster 1–4；Cluster 0 只完成治理、current-source characterization 与人工批准门。Cluster 0 未获项目 owner 明确批准前，业务代码一律 **NO-GO**。Cluster 5 复用该闭环增加目录选择与单文件 profile。

固定顺序：

```text
Cluster 0 R/D/T + ADR + characterization + 人工批准
  → Cluster 1 身份与 origin
  → Cluster 2A 聚合/reconciliation
  → Cluster 2B save/recovery
  → Cluster 2C ProjectPackage logical + physical 闭环
  → Cluster 3 应用服务
  → Cluster 4 Qt 与 current-source acceptance
```

每个 Cluster 只形成一次阶段性 Git 提交，并由独立 reviewer 对累计 diff 做对抗审查。Cluster 2 的 2A/2B/2C 是同一 Promotion Cluster 内的顺序门；三段都闭合后才形成 Cluster 2 提交。不得把下一 Cluster 的 production 变化抢跑进当前提交。

## Cluster 0：治理、Characterization 与人工批准

- [x] 0.1 完成中文 Requirements 与 Research
  - 冻结 `Project → Document → Segment`、三类 origin、复合身份、保存/reconciliation、ProjectPackage 与 UI 边界。
  - 记录当前单 JSON/TXT project、Parser/Codec、Controller/Search 与 Qt 行为基线及已知兼容入口。
  - 明确本规格不拥有 TM/术语 ResourcePackage、TMX、RPY 语法、PO/POT writer、CONTEXT、chunk 或 sync provider。

- [x] 0.2 完成中文 Design 并复核已采纳 ADR-018
  - 设计 immutable contracts、聚合边界、稳定身份、dirty/save/reconciliation、逻辑 manifest、package 事务、Controller 与 Qt 投影。
  - ADR-018 记录 ProjectPackage authority、`codec_private_member` 不透明边界与 package/import/apply 事务；Cluster 0 必须证明 R/D/T/border 与该已采纳决策一致。
  - 物理 carrier 决策须经过 2C 实现前的人工门；现行 carrier 由 ADR-019 定义，2C 实现须保持同一逻辑合同。

- [x] 0.3 冻结 Tasks、review clustering、border 与所有权
  - 保持 brief 的 Cluster 1–4 编号和依赖；Cluster 2 明确拆为 2A/2B/2C。
  - 每 Cluster 一次提交、一次独立对抗 review；失败时只做窄 remediation，不重写已审计的前序 Cluster。
  - `feature/multi-document-project-workspace` 只写 owning Spec；共享 ADR、review/border 与 Steering 由治理 owner 同步。

- [x] 0.4 建立 current-source characterization
  - 枚举 `editor_contracts.py`、`editor_project.py`、`editor_controller.py`、`project_search.py`、`workspace_state.py`、`parser_contracts.py`、`parser_composition.py`、`parser_source.py`、`qt_editor.py`、`qt_editor_window.py` 的 import/constructor/call/patch/serialization consumer。
  - 冻结单 JSON/TXT 打开、编辑、搜索、dirty、保存、重开、故障和 Qt session 的 current-source 行为；记录真实 fixture 与 deterministic digest/inventory。
  - 建立负向 architecture guards：Parser/Codec 不聚合 workspace，workspace 不解释 `codec_private_member`，chunk/sync/resource/TM authority 不反向进入。

- [x] 0.5 取得 Cluster 0 人工批准
  - Requirements、Design、Tasks、ADR-018、review clustering、border 与 characterization 全部由独立 reviewer 审查。
  - 项目 owner 明确批准后才可勾选本项并开始 Cluster 1；无批准即保持业务代码 NO-GO。

### Cluster 0 完成门

- 0.1–0.5 全部完成且人工批准范围可从 `spec.json` 核对。
- production、产品 UI 与 current-source evidence payload 零变化。

## Cluster 1：身份与 Origin（brief Promotion Cluster 1）

- [x] 1.1 建立 Project / Document / Segment immutable contracts
  - `ProjectDocument` 使用稳定 `document_id`；项目内 segment identity 固定为 `(document_id, local_segment_id)`。
  - display name、sheet name、列表位置和枚举顺序不得成为持久身份。

- [x] 1.2 建立 `single_file` / `directory` / `workbook` origin 与稳定 `source_ref`
  - 规范化相对 `source_ref` 与 manifest-issued ID 的优先级、路径安全和重复/冲突拒绝语义。
  - origin 只描述项目来源，不据此宣称任意 XLSX、RPY、PO/POT 或其他 codec 已可写。

- [x] 1.3 建立既有单 JSON 兼容适配
  - 既有单 JSON 行为可投影为一个 Project/一个 Document，但不得改变其 current-source open/save/segment identity 语义。
  - Workspace v1 eligibility 在 adapter 提升时执行；不合规旧文件结构化拒绝且原字节不变，legacy `load_project()` / `save_project()` 仍完全兼容。
  - 当前单 JSON 路径继续可独立工作，不把本规格变成 Parser Foundation 的反向前置。

- [x] 1.4 闭合身份与 origin 的对抗测试
  - 覆盖重命名、重排、同名文档、路径规范化、重复 ID、伪造复合 ID、跨项目身份与旧入口兼容。
  - 独立 reviewer 证明没有临时路径/显示名/列表索引权威。

### Cluster 1 完成门

- immutable identity 与三类 origin 通过 current-source compatibility 和 hostile fixtures。
- Cluster 1 独立对抗 review 通过后才进入 Cluster 2A。

## Cluster 2：聚合、持久化与手工 ProjectPackage 闭环（brief Promotion Cluster 2）

### Cluster 2A：Aggregation / Reconciliation

- [x] 2.1 建立 document ordering 与连续 workspace 聚合
  - manifest/import 顺序只决定显示和导航顺序，不改变 Document/Segment 身份。
  - workspace 组合 codec 已产生的单文档结果，不复制格式 grammar 或 writer。
  - 提供有界显式文件 intake：用户选择同一 portable root 下的 JSON/TXT/PO/POT 列表，逐个取得 Parser verified terminal 后才聚合；不扫描目录、不自动包含相邻文件、不赋予 reader-only writer。
  - 保留同一 root fd 和所有 selected regular-file identities 至整批 terminal 完成，拒绝 hardlink/symlink/root replacement/file drift；发布的 staged DTO 必须明确 `durable=False` 且原 source bytes不变。
  - 冻结 flat segment 投影、document/project progress 与 deterministic workspace content digest；现有 Document 不得因 incoming selection reorder 改变顺序，真正 new Document 只按 incoming 顺序追加。

- [x] 2.1a 将 selected-source root 与 identity proof 接入 `RootedFileSystem`
  - Windows 批量 intake 保持同一 retained portable root 与全部 selected regular-file handles 至整批 terminal；reparse/hardlink/root replacement/file drift 均在 workspace publication 前拒绝。
  - live Windows Volume/FileId只用于本次批量duplicate/reparse证明；持久`OriginBinding`保存non-authoritative observation并在每次操作fresh root bind后以content/owner binding复证。
  - _Amendment: WA-03_
  - _Depends: 2.1, ADR-020, WA-01, windows-platform-enablement 3.7_

- [x] 2.2 建立 source reconciliation
  - 按稳定复合 ID 与 source fingerprint 产生 `unchanged`、`source_changed`、`new`、`removed`、`ambiguous`、`unresolved`。
  - `source_changed` 保留 target 但撤销确认；`removed`、`ambiguous`、`unresolved` 保留恢复引用并要求显式处置，不按列表索引或正文相似猜测。
  - 冻结设备本地 `OriginBinding` 的 exact root/source_ref/revision → document_id 回接；新 source identity 允许变化并参与 reconciliation，preview 后再变才 stale。重命名只经显式映射，forged/stale/cross-root binding 在首次 mutation 前拒绝。
  - `keep_detached` 后 detached-only Document 留在 workspace，但不伪造 live `OriginBinding`；apply 仅消费当前 service 签发的一次性 operation，必须复验 project/session/revision/workspace/source identities 后才单次 swap。

### Cluster 2B：Save / Recovery

- [x] 2.3 建立 carrier-neutral save candidate、LKG 与结构化报告
  - 冻结逐 Document baseline、完整 candidate、staging/validation/publication/readback 与 last-known-good 语义；只清除已证明持久化的 dirty。
  - 报告区分 `saved`、`rolled_back`、`unchanged`、`failed` 与不确定/需恢复状态，不用项目级布尔值抹平局部结果。

- [x] 2.4 闭合 save/recovery fault model 与未来 origin 原子性红线
  - 覆盖 candidate、validation、publication、readback、commit、rollback 与 cold recovery fault；任何不确定状态保持 LKG、dirty 与恢复信息。
  - directory/workbook 仅冻结后续 profile 必须遵守的逐文档 journal/单文件原子替换红线；本 Cluster 不启用 directory discovery、workbook project profile 或新 source writer。

- [x] 2.4a 将 workspace/package save 绑定到平台 lease 与 publisher
  - Windows 保存必须在同一 root/target binding 与 `ProcessFileLock` lease 下完成candidate write-through、`FlushFileBuffers`、validation、handle-bound atomic naming、candidate close和retained destination exact bytes/digest readback；owner随后提交receipt/LKG并完成terminal reproof，失败保持LKG、dirty和恢复事实。
  - Project owner保留deterministic ZIP、target-hashed exact journal/LKG/candidate、receipt、dirty/baseline与recovery；所有package read/write按至多64 KiB流式处理，clean reopen不扫描residue。
  - _Amendment: WA-03_
  - _Depends: 2.4, ADR-020, ADR-025, WA-01, windows-platform-enablement 3.7_

### Cluster 2C：ProjectPackage Logical + Physical Closure

- [x] 2.5 建立版本化 `ProjectPackageManifest` 与 carrier-neutral package contracts
  - manifest 固定项目/文档身份、顺序、member reference、版本与 digest；未知版本、重复 member、digest/identity/path 冲突 fail closed。
  - document content 与 `codec_private_member` 只作为带 digest 的 member；通用 workspace 不解释 codec-private bytes。
  - preview 无写入且与最终 apply 消费同一已验证计划；receipt 对账 package identity/version/member digest、reconciliation 与逐文档结果。
  - 不把 TM/术语资源装入 ProjectPackage，也不建立 ProjectPackage/ResourcePackage 共同 authority。

- [x] 2.6 在任何 2C production 实现前批准 ProjectPackage 物理 carrier 决策
  - 用 current-source prototype/fixture 比较目录、单文件 archive 或其他候选的确定性、原子替换、路径安全、流式校验与恢复语义。
  - carrier、版本兼容与输入拒绝边界须经 owner 审阅批准后才能进入 C2C 实现；若需新 ADR，则新增后继 ADR，不改写 ADR-018 的逻辑包权威。未批准时 2C implementation NO-GO。
  - 依据 ADR-019：v1 唯一 carrier 为严格闭集的 `localcat-project-package-zip-v1`/`ZIP_STORED`；拒绝 ZIP64、压缩、宽松 `zipfile` 读取和并行 directory reader/writer。

- [x] 2.7 实现手工 export / validate / preview / import / apply / receipt
  - export 只在完整 staging、member digest 和 readback validation 成功后发布，不完整导出不得覆盖旧包。
  - import 复验物理 carrier 与逻辑 manifest，并让 preview 后的 apply 使用同一事务/计划；失败保留旧项目与可重试信息。

- [x] 2.8 闭合冷重开与 package fault matrix
  - 覆盖截断、重复/缺失/额外 member、路径穿越、digest/version/codec声明不符、preview 后篡改、commit/readback/reopen 失败。
  - live codec unavailable 只产生 body-safe warning并禁止source write-back，不阻止package离线导入/target编辑；只有声明损坏或请求解释private member的操作才fail closed。
  - 用至少两个 Document 且跨文档复用同一 local segment ID 的真实 ProjectPackage 冷重开，逐项核对项目/文档/segment 身份、顺序、source/target、opaque member 与 receipt。

- [x] 2.8a 闭合 Windows ProjectPackage 冷重开与恢复矩阵
  - 在真实local fixed NTFS上覆盖package source/destination replacement、锁竞争、各publication fault、process kill、app restart与正常OS reboot；恢复只接受完整old、完整new或recovery-only，并继续严格验证ADR-019 ZIP carrier、foreign inode/FileId与dirty/baseline。
  - _Amendment: WA-03_
  - _Depends: 2.1a, 2.4a, 2.8, ADR-025, windows-platform-enablement 3.7_

### Cluster 2 完成门

- 2A/2B/2C 各自通过定点对抗检查，累计 diff 再通过 Cluster 2 独立 reviewer。
- `collaborative-job-chunks` 的统一进入门位于完整 Cluster 2 之后；不得在仅有 Cluster 1 identity 时开始 chunk schema/权限实现。
- Cluster 2 后恢复/确认 `language-resource-portability` brief，再提升 TM JSONL 与术语 CSV/v1 ResourcePackage R/D/T；`tmx-context-interchange` 未来只拥有可选 TMX export profile。两项不得互相冒充或抽取 ProjectPackage 共同 authority。

## Cluster 3：应用服务（brief Promotion Cluster 3）

- [x] 3.1 建立 Controller workspace session 与 issued identity
  - open/switch/edit/save/reconcile/package 操作只接受当前 session/generation 签发的 Project/Document/Segment 身份。
  - stale/forged/cross-project identity 在任何 mutation 前 fail closed。

- [x] 3.2 建立项目/文档 dirty 与保存状态投影
  - 文档状态聚合为项目状态但不抹平逐文档失败；保存成功只清除已证明持久化的 dirty。
  - package import/apply 与 source reconciliation 使用 Cluster 2 冻结的事务和 receipt。

- [x] 3.3 建立可扩展 search scope
  - 首批只开放 `current_document` / `entire_project`，UI 文案为“当前章节 / 搜索全部章节”。
  - 可保留未来 `current_chunk` enum 扩展位，但不得映射成 Document、查询未实现 chunk 或暴露 chunk 控件。

- [x] 3.4 闭合应用层并发、故障与兼容矩阵
  - 覆盖切换章节、session 替换、reconcile/save 竞态、stale search result、部分保存与旧单 JSON controller journeys。
  - 独立 reviewer 证明 Controller 不解释 codec grammar/private member，也不拥有 provider/chunk/TM authority。

### Cluster 3 完成门

- application 只消费 Cluster 1–2 contracts；失败不丢失 dirty、identity 或恢复信息。
- 历史 C0 runtime source digest 的漂移已由当时的 C4 final-roots 验收收束。
- Cluster 3 独立对抗 review 通过后才进入 Cluster 4。

## Cluster 4：Qt 与 Current-source Acceptance（brief Promotion Cluster 4）

- [x] 4.1 实现章节导航与连续段落体验
  - 显示章节名/当前位置，支持跳到章节首段；重命名/重排只改变投影，不改变持久身份。
  - 顶栏无下拉箭头的文件夹菜单是唯一文档导航 authority；菜单条目使用文件图标和文档名，不添加序号前缀。编辑左栏和浏览/校对标题只投影当前文档，列表使用带文件图标的跨列文档标题分隔段落；浏览行变化必须同步当前文档与菜单选中态。
  - 窄宽布局不隐藏文件夹导航、项目命令或保存失败状态；ProjectPackage 打开/导入只保留在“项目”菜单，不占用文件夹或独立顶栏。
  - 首页、项目主按钮、项目菜单唯一的“打开本地项目”和 `Ctrl+O` 不拆分单/多文档入口：单选直接打开，Shift 多选进入排序/语言/保存确认；文件直接拖到首页时按相同规则分流。项目菜单不得另列“新建多文档项目”。多选只消费 Cluster 2A 的有序显式文件集，不在 Qt 复制 suffix/Parser grammar、扫描目录或自动包含相邻文件。
  - ProjectPackage `open` 直接冷打开所选artifact；`preview+import`绑定source/destination，以LocalCAT对话框显示模式、当前→incoming、单行可复制ID、计数/调和/阻断，Cancel默认且Apply后才发布。Legacy会话要求另选package destination，preview不创建目标、不改原文件、不切换会话，Apply才以`NEW`/`REPLACE`导入并切换，不把Legacy内容冒充为merge/promotion。

- [x] 4.2 接入搜索范围、dirty 与保存/recovery 反馈
  - UI 只通过 Controller 消费 `current_document` / `entire_project`、逐文档保存报告和 reconciliation/receipt。
  - 不在 Qt 复制 manifest、codec、reconciliation、package 或 identity authority。

- [x] 4.3 使用真实 ProjectPackage substrate 完成 current-source acceptance
  - acceptance 从 Cluster 2 正式 exporter 生成真实 ProjectPackage，经 validate/preview/import/apply 后冷重开，再运行章节导航、编辑、搜索、保存与恢复 journeys。
  - 不得用内存伪 package、手写 manifest 或 fixture-only controller 注入代替真实 substrate。

- [x] 4.3a 执行 Windows hostile-path 与项目保存/重开 journey
  - 通过 production Controller/Qt 在源码运行下完成多文档打开、编辑、保存、关闭、冷重开与 recovery feedback，并覆盖 junction/reparse、ancestor/target replacement 和 target-open 拒绝；逐项核对 identity、dirty、target 与 receipt。
  - _Amendment: WA-03_
  - _Depends: 2.8a, 4.3_

- [x] 4.4 重签 current-source evidence 并完成治理收尾
  - 在 final runtime roots 冻结后运行全量 identity/reconciliation/save/package/controller/Qt/fault/acceptance 与单 JSON/TXT compatibility suites。
  - 当时由 owner 工具生成、strict consumer 复读 evidence，并冻结该次验收范围。
  - 同步真实结构/技术事实并由 Cluster 4 独立 reviewer 给出 Feature GO/NO-GO。

- [x] 4.4a 执行 ProjectPackage 双进程与文档化恢复边界
  - 通过真实ProjectPackage业务API重放4.3a，注入第二进程占锁、owner kill与每个publication phase故障；app restart与正常OS reboot只接受完整old、完整new或recovery-only，并核对owner receipt/LKG、dirty/baseline与terminal reproof。突然断电资格属于未来独立证据，最终frozen Qt journey留给WA-08消费。
  - _Amendment: WA-03_
  - _Depends: 4.3a, ADR-020, ADR-025, windows-platform-enablement 3.7_

- [x] 4.5 完成 Browse/Review 分组轮次 UI increment
  - 在 Browse/Review 主页表格左侧嵌入当前文件的可滚动轮次导航，同时压缩“段落”列；默认使用自动收起式窄指示条，设置中可切换为固定式预览列表，标题行按钮只打开设置。默认每组 20 段，严格“超过 5 组或 100 段”显示，尺寸只允许 20–200/步长 10。
  - 两种显示方式共享同一 projection 与 current group；自动收起式 hover/focus 后显示组首段预览，固定式常驻预览。使用 1+3 行或 source-only 4 行规则，点击只按 issued Segment identity 跳转，不引入分组持久化 authority。
  - 用高对比应用自有文件图标替换平台白色图标，并清除所有已知 Qt 标准弹窗的英文默认按钮。
  - 当时执行 contract/state/Qt 键盘跳转/滚动/视觉/中文按钮回归、final-tree current-source evidence 与独立对抗复核。

### Cluster 4 完成门

- 至少两个 Document 的真实 ProjectPackage 冷重开与 Qt current-source journeys 全部通过；directory/workbook 产品 profile 继续保持未启用的负向边界。
- evidence 绑定同一 final runtime tree；任一 package、identity、保存或兼容失败均为 NO-GO。

## Cluster 5：显式根目录选择与嵌套导航

Requirement 13 的目录选择消费平台观察端口并向 Qt 投影；RPY 格式能力由独立规格提供。5.6 是基于既有显式文件 intake 与 ProjectPackage 的独立任务，可先于目录观察和预览的 5.3–5.5 执行；5.6a 再集成目录预览所保留的原根。其余目录任务仍按下列依赖逐项执行。

- [x] 5.1 实现平台 owner 的只读观察合同
  - 定义受限 metadata、retained-root 下钻、取消/资源预算和结构化失败；只读能力不借用 live ledger 或写 authority。
  - 完成时，平台 fake 能验证 handle 生命周期和超限/取消行为，Project 不需要裸路径枚举。
  - _Requirements: 13.1, 13.3_
  - _Boundary: Platform observation contracts_
- [x] 5.2 实现 POSIX 只读目录观察
  - 从 retained root 观察和下钻，拒绝 symlink/ancestor drift，及时关闭非祖先 handle，不写源目录。
  - 完成时，POSIX 合成目录的替换、深度/条目/handle 限额及取消返回明确结果。
  - _Requirements: 13.1, 13.3_
  - _Boundary: Platform POSIX backend_
- [ ] 5.3 实现 Windows 只读目录观察
  - 使用同一合同处理 retained root、reparse/ancestor drift 与资源释放，不以字符串前缀冒充根约束。
  - 完成时，Windows 合成 junction/reparse、替换、限额及取消矩阵符合相同失败语义。
  - _Requirements: 13.1, 13.3_
  - _Boundary: Platform Windows backend_
  - _Depends: 5.1_
- [ ] 5.4 验证平台观察到 Project 的集成合同
  - 以同一中立 consumer 运行两个 backend 的正反例，验证 metadata 观察不读取正文或授予 source writer。
  - 完成时，条目/根身份事实、关闭后使用和取消行为一致，平台不泄漏额外路径权限。
  - _Requirements: 13.1, 13.3, 13.6_
  - _Boundary: Platform / Project integration validation_
  - _Depends: 5.2, 5.3_
- [ ] 5.5 实现 Project 目录预览与显式选择服务
  - 建立 immutable 候选树、registry 中立能力筛选、确定顺序与 issued selection；默认不选，只有确认选择进入内容读取。
  - 完成时，两层及更深目录/同名文件可预览，未选内容零读取；不完整扫描/越界/alias/过期选择阻断，筛选不重算根。
  - _Requirements: 13.1, 13.2, 13.3_
  - _Boundary: Project discovery/contracts_
  - _Depends: 5.4_
- [ ] 5.6 (P) 实现直接文件选择的单文件 profile 与包验证
  - 基于既有 rooted 显式文件 intake 增加 explicit-single-file-v1 的一文档 cardinality、OriginBinding 与 package decoder/validator；使用文件选择已有的根绑定规则和规范化 source_ref，不引入 metadata 递归观察或目录预览。
  - 完成时，既有 JSON/TXT codec 可验证零文件拒绝、一个文件建立非 legacy 包、首包发布及无原路径冷重开保留 identity/overlay；多个文件的原 profile 限制与 legacy 单 JSON 行为不变，不授予源 writer。
  - 验证源/根替换、包发布失败与两个平台既有文件读写路径；Windows 的原生读写验收仍须完成，不以目录观察尚未实现为由跳过。此任务唯一提供 RPY 1.4 消费的 profile，不依赖 RPY 私有 handoff。
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 4.1, 13.3, 13.4_
  - _Boundary: Project contracts/intake/package_
  - _Depends: 2.1a, 2.4a, 2.8a_
- [ ] 5.6a 接入目录选择的原根绑定与单/多文件 intake
  - 消费 5.5 的 issued selection 和 retained root，按原根生成 source_ref；一个文件复用 5.6 的 profile，多个文件沿用既有 profile，不因单选或筛选重新计算根。
  - 完成时，JSON/TXT 合成目录中零选拒绝、嵌套单选保留完整相对路径、同名多选与顺序可冷重开恢复；stale/越界/alias/取消不提交，未勾选内容零读取。
  - _Requirements: 13.2, 13.3, 13.4_
  - _Boundary: Project discovery / intake integration_
  - _Depends: 5.5, 5.6_
- [ ] 5.7 接入统一打开流程的根目录勾选 review
  - Controller/Qt 接入选根、候选树勾选、排序/语言/保存 review，复用现有单/多文件入口；异步取消与 session/generation 复证。
  - 完成时，根目录单选/多选均可保存，零选/不完整/stale 阻断，取消或迟到结果不改变当前 target/dirty，原文件多选仍可用。
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.6_
  - _Boundary: Controller / Qt open-flow integration_
  - _Depends: 5.6a_
- [ ] 5.8 将既有文件夹导航投影为目录树
  - 只使用 Controller-issued identity/source_ref 构树，沿用 current/dirty、键盘跳转与 Chunk 过滤；目录仅展开。
  - 完成时，包离线冷重开仍可导航，同名文件可区分；目录交错顺序不改变 manifest、连续阅读或搜索次序。
  - _Requirements: 13.2, 13.5, 13.6_
  - _Boundary: Controller / Qt navigation projection_
  - _Depends: 5.6a_
- [ ] 5.9 纳入普通发行模块组合
  - 将新增发现/合同模块纳入 source 与普通 frozen 的显式声明，不扩张 Core/Host/Gate 合同。
  - 完成时，普通 Windows frozen 能打开目录预览及章节树，缺源目录时包导航仍可用。
  - _Requirements: 13.5, 13.6_
  - _Boundary: Platform build composition_
- [ ] 5.10 执行增量端到端与故障回归
  - 执行 source 与 Windows 嵌套目录/包/Qt 旅程及原文件入口回归；合成 fixture 不依赖个人目录。
  - 完成时，未选内容不读、drift/取消不提交、包冷重开和目录树状态均有当前实现证据，原 profile/writer 负向边界保留。
  - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_
  - _Boundary: Project / Platform / Qt acceptance integration_
  - _Depends: 5.7, 5.8, 5.9_
- [ ] 5.11 闭合实际治理同步与下游消费
  - 由治理 owner 同步实际实现的目录/单文件边界；RPY 消费此 profile 与选择服务，不复制枚举或持久化。
  - 完成时，实际 diff 与现行合同一致、平台/Parser/Qt 相关回归可核查；未实现项不列为完成，不改写历史验收。
  - _Requirements: 13.4, 13.6_
  - _Boundary: Governance / downstream integration closure_
  - _Depends: 5.10_

## 相邻规格边界

- 恢复/确认 `language-resource-portability` brief 后，提升独立 R/D/T，拥有 TM JSONL 与术语 CSV/v1 ResourcePackage、报告和冷重开；sync 分别消费ProjectPackage/ResourcePackage，不复制 live SQLite、journal、sidecar 或 staging residue。
- `tmx-context-interchange` 只拥有 ResourcePackage 未来可增加的 TMX export profile、TMX context/provenance 与有损取舍；TMX 不是 ProjectDocument，也不由本规格开放 TMX writer。
- `rpy-project-codec` 独立拥有 RPY token/sidecar/占位符与 writer；folder 接入依赖本规格，但手工包闭环按 ADR-030 先于 sync。
- PO/POT reader 或未来 canonical writer 归独立 format codec；本规格的 `single_file`/`directory` origin 不自动赋予 PO/POT writer 能力。
- CONTEXT 精确语义及“上下文一致”UI 投影归 `feature5-ui-integration`（Integration TM surface）；本规格不增加 evidence 字段或匹配判定。
- `collaborative-job-chunks` 在完整 Cluster 2 后才可开始，只引用稳定 segment membership，不拥有文档身份、项目保存或远程传输。

## 架构验证维护

- [x] 4.6 用直接架构与行为断言替代本 owner 的静态源码快照
  - 读取实际 owner 源码检查禁止依赖，使用静态、别名和字面量动态导入反例；保留身份、序列化、权限和发布的既有业务测试。
  - 删除本 owner 的机械快照、专用生成器及跨 JSON 摘要消费；历史执行报告保留原字节，按原 Git 范围解释引用。
  - 与 Multi-Document、Chunk、ResourcePackage、TMX 的对应维护项组成一次集成，逐 owner 收束消费者；Parser 架构测试只同步已删除工具和源码枚举，不放宽 owner 规则。
  - 退出条件：本 owner 直接测试、Parser 源码枚举及已删除工具分类检查通过，活动消费者不再要求旧快照；fixture、Gate 与运行时兼容性依据保持不变。
  - _Requirements: 12.1、12.5、12.7、12.9_
  - _Boundary: 本 Spec 测试/合同及其静态快照、生成器；跨 owner 仅处理这四项共同的快照消费依赖_

## 明确禁止

- Cluster 0 人工批准前修改任何 production/runtime/UI/evidence payload。
- 用显示名、sheet 名、文件枚举顺序、列表索引或临时绝对路径充当稳定身份。
- 让通用 workspace、chunk、sync provider 或 Qt 解释 `codec_private_member`。
- 在 2C carrier 决策获批前实现或暗定 archive/directory 物理形态，或使用 ADR-019 之外的 carrier。
- 把 ResourcePackage 与 ProjectPackage 抽象成共同 authority，或让 sync 直接复制 live canonical store。
- 抢跑 TMX export、RPY product rollout、PO/POT writer、CONTEXT UI、chunk 权限或 remote provider。


## Implementation Notes

- 目录观察 backend 须在获取原生目录 handle 前通过 `DirectoryHandleBudget` 按实际数量预留，包含复制的祖先链与临时枚举资源；不能只按 authority 对象计数。原生关闭未证实成功时保留占用，不因 authority 已终止而归还额度。
- POSIX 观察从文件系统锚点逐组件 no-follow 绑定，根的命名祖先也不接受符号链接别名；发现服务不得静默 resolve 来绕过拒绝。目录流使用可检查 `closedir` 结果的私有原语，避免标准库吞掉关闭错误；目录 generation 变化即作废本次观察。
