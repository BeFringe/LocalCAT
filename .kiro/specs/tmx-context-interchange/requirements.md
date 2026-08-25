# 需求文档

## 简介

LocalCAT 已有拒绝 DTD/ENTITY、限额流式读取和语言对选择的 TMX Level 1 reader，但还没有 canonical writer；导入会丢弃 `<prop>`，项目、分工和 managed TM resource 也没有边界清晰的 TMX 导出。若直接把参考平台的 job TMX、ResourcePackage 或项目保存混成一个操作，将同时破坏 Workspace、Chunk、Resource 与 carrier 的 authority。

本规格建立一个 TMX Level 1 互操作 profile。它拥有 TMX payload grammar、context/provenance/vendor prop 映射、inclusion/loss policy、direct `.tmx` preview/publication；消费 Resource、Workspace、Chunk 签发的 exact scope facts。Managed resource 可选择 direct `.tmx` 或由 `language-resource-portability` 封装的 ResourcePackage 后继 profile；项目和分工始终 direct-only。

## 范围边界

- **范围内**：TMX Level 1 安全导入；prop 原始保留与受控语义映射；managed resource / entire project / selected chunk 三种 export adaptation；deterministic writer；loss report；direct preview/publication/recovery；ResourcePackage TMX payload handler；Controller/Qt 入口。
- **范围外**：ProjectPackage、Workspace/Chunk identity 或 membership 生成、ResourcePackage manifest/ZIP/preview/apply/receipt、provider/网络、MyMemory API、Fuzzy、复杂内联标签编辑、Job 身份。
- **上游**：Parser 安全 reader；canonical TM snapshot；Workspace session/universe projection；Chunk exact scope projection；ResourcePackage carrier/transaction owner。

## 术语

- **Source_Scope**：`managed_resource`、`entire_project` 或 `selected_chunk`。
- **TMX_Profile**：`localcat-tmx-level1-context-v1`，定义 TU/TUV、locale、prop、顺序、限额与 loss policy。
- **Effective_Locale**：预览中明确绑定的源/目标语言；上游为 `und` 时 UI 以灰字建议 `en` / `zh-CN`，用户可改，writer 不自行猜测。
- **Loss_Report**：按稳定 reason code 统计 included、excluded、warning、blocking facts，不包含正文。
- **Direct_Publication**：绑定 source scope 与 destination before-fact、stage、冷验证、原子发布、读回与 receipt 的 `.tmx` 事务。

## 需求

### Requirement 1：三轴 Authority

1. The implementation shall 独立表达 source scope、TMX payload profile 与 carrier，不用一个枚举或 UI 状态替代三者。
2. Resource shall 只签发一个 managed TM 的完整 canonical snapshot；Workspace shall 签发 current project/session/revision 的正文、顺序和 universe；Chunk shall 只签发一个明选 active chunk 的 exact membership。
3. The TMX layer shall 不从路径、display name、列表位置、source 文本或相似度重建 scope。
4. `managed_resource` shall 支持 direct `.tmx` 与 ResourcePackage；`entire_project` / `selected_chunk` shall 只支持 direct `.tmx`。
5. The TMX layer shall 不拥有 ProjectPackage、ResourcePackage container/apply/receipt、Workspace/Chunk identity 或 provider。

### Requirement 2：安全 TMX reader 与 prop 保留

1. The Parser TMX reader shall 保持 DTD/ENTITY、inline XML、编码、深度、字段和总量限额的 fail-closed 基线。
2. When 读取 TU/TUV `<prop>` 时, the reader shall 以稳定顺序保留 type、xml:lang 和文本值；重复 type 不得折叠。
3. The semantic importer shall 只把 profile registry 已批准的属性映射为 context/provenance/status；未知 prop 以原始 metadata 保留，不猜测上下文。
4. `x-MateCAT-status` shall 可映射为 provenance/status；没有真实证据的 MateCat/MyMemory prop 不得映射为 canonical context。
5. When TMX 缺少 context prop 时, import shall 仍成功并报告 context unavailable，而不是损坏。
6. Parser shall 继续是唯一通用 TMX 安全 reader；本规格不得另建宽松 XML reader。

### Requirement 3：确定性 TMX payload

1. The writer shall 生成 UTF-8、LF、无 DTD/ENTITY 的 TMX 1.4 Level 1 文本，Header 明确携带 `creationtool`、`creationtoolversion`、`segtype`、`o-tmf`、`adminlang`、`srclang` 与 `datatype`，并以稳定 TU/TUV/prop/attribute 顺序输出。
2. Each exported unit shall 有稳定 scope-derived TU identity；重复 source/target 单元不得去重或合并。
3. The writer shall 使用 preview 绑定的 exact source/target effective locale，不从文本或系统 locale 猜测。
4. The writer shall 转义 XML 文本并拒绝无法在 profile 中无损表示的控制字符或 inline XML。
5. For 相同 profile、locale 与 ordered units, output bytes and digest shall 相同；路径、operation id、当前时间不得进入 payload。
6. The staged file shall 由 Parser reader 冷重开，并复证 TU count、locale pair、metadata/loss facts 后才可发布或交给 carrier。

### Requirement 4：Inclusion 与 Loss Policy

1. Empty-target units shall 被排除并按 `empty_target` 计数，不签发空译文 TU。
2. Non-empty unconfirmed units shall 被包含并按 `unconfirmed_target` 警告；source 等于 target shall 被包含并按 `source_equals_target` 警告。
3. Detached chunk members shall 保留在 Chunk projection，但由 Workspace join 后排除并单独计数；不得静默丢弃。
4. Missing、foreign、stale scope binding 或无法无损编码的 required metadata shall 在 destination mutation 前 blocking fail。
5. Unknown prop shall 在 representable 时按原顺序/重复项导出；若无法无损表示则进入 blocking loss，而不是静默丢弃。
6. Preview/receipt shall 只公开 counts、stable codes 和 bounded safe issues，不包含 source/target/context/provenance 正文。

### Requirement 5：Managed Resource Scope

1. A resource export shall 捕获一个 active canonical TM 的 complete ordered snapshot，并绑定 store identity、generation、revision、record count 与 digest。
2. TM records 的 speaker、context_prev/context_next、file_source 与 provenance shall 投影到 profile-owned props；未知 imported props shall 继续保留。
3. Publication 前 shall 重新证明同一 resource lifecycle、generation/revision 与 snapshot digest；stale 时零 mutation 拒绝。
4. Resource preview shall 不受当前 project/document/chunk 或搜索状态影响。

### Requirement 6：Entire Project Scope

1. Project export shall 同时绑定 `WorkspaceSessionView` 与 owner-issued `WorkspaceUniverseProjection` 的 project/session/revision/composition/digest facts。
2. Units shall 按 Workspace project/document/segment navigation order 投影，并以 exact `(document_id, local_segment_id)` join presence/content。
3. Project scope shall 包含所有 attached segments；chunk selection、current document、search 或当前行不得隐式缩小范围。
4. Publication 前 shall 重新签发并复验同一 Workspace binding、order、presence 和 content digest；任何 drift 零 mutation 拒绝。

### Requirement 7：Selected Chunk Scope

1. Chunk export shall 要求一个明选 active chunk ID，并绑定 project/session、workspace universe、plan id/revision/digest/universe digest 与完整 exact membership。
2. Exactly one chunk shall 被导出；不得把多选 chunks 合并为一个未声明 scope，也不得把 current UI state 当持久授权。
3. Workspace shall 按 exact segment identity join 当前 content/order/presence；Chunk 不携带正文或决定 inclusion。
4. Publication 前 shall 同时复验 Workspace 与 Chunk projections；missing/foreign/stale membership blocking fail，detached 仅按 Requirement 4 排除并计数。
5. LocalCAT shall 使用“分工”而非虚构 Job identity。

### Requirement 8：Direct Preview、Publication 与 Recovery

1. Preview shall 绑定 exact scope facts、profile、effective locales、destination parent identity 及 absent/existing regular-file before-fact，并签发 private single-use plan。
2. Project/chunk preview shall 显示 scope badge、project ID、可选 plan/chunk binding、文档/attached/included/excluded/warning counts、locale、profile 与目标路径。
3. Resource preview shall 显示 resource identity、generation/revision、included/excluded/warning counts、locale、profile 与目标路径。
4. Apply shall 在同一已绑定 parent 创建 exclusive candidate，fsync/close、Parser cold validate 后原子发布；existing target 使用可验证 LKG。
5. Pre-publication failure shall 保持 destination exact 不变；post-publication readback failure shall 仅在能证明 target 仍是本 operation candidate 时恢复 prior，否则返回 recovery-required。
6. Cancel shall 不创建或修改 destination；preview 不可序列化或重放。
7. Success shall 返回 body-safe receipt，绑定 scope/profile/locale/destination before/after digest/counts 与 durable outcome。

### Requirement 9：ResourcePackage TMX 后继 Profile

1. The successor profile shall 以新的 exact schema/carrier/profile-set triple 批准 `translation_memory/localcat-tmx-level1-context-v1`，不得静默宽化 v1 JSONL/CSV profile set。
2. The package shall 正好携带一个 managed TM resource 的完整 TMX snapshot；项目/分工 scope shall 被 capability matrix 拒绝。
3. TMX module shall 只提供 deterministic payload handler 与 cold validation；ResourcePackage owner 继续拥有 manifest、ZIP、destination publication、validate/preview/apply/receipt/recovery。
4. The ResourcePackage module shall 不实现 XML/TMX grammar；TMX module shall 不创建 package manifest、ZIP、apply plan 或 ResourceOperationReceipt。
5. The successor TMX profile shall 首轮只批准 export/cold-validate publication；ResourcePackage import/apply 继续只接受既有 JSONL/CSV profile，不能因 XML 可读而宽松启用 TMX replace。

### Requirement 10：Qt 产品入口

1. Project menu shall 只有一个“导出项目”入口，不带省略号，不在欢迎首页增加固定控件。
2. Project export dialog shall 明确选择“整个项目”或一个 active chunk，使用边界清楚的 preview card；不得出现 ProjectPackage 的 REPLACE、reconciliation 或应用导入语义。
3. Resource page `⋮` shall 独立提供“导出 TMX”与“导出资源包”；managed-resource TMX 范围不跟随 project/chunk。
4. Export shall 在非 GUI 线程执行并服从资源/项目 busy gate；worker/cancel/failure 必须恢复交互。
5. Qt shall 只消费 Controller frozen projection/commands，不解析 TMX XML、scope projection、ResourcePackage 或 owner store。

### Requirement 11：兼容与完成证据

1. Existing Legacy/TMX import、TM exact/context/fuzzy、JSONL/ResourcePackage、termbase、ProjectPackage、multi-document 和 chunks journeys shall 保持通过。
2. Acceptance shall 用真实 canonical TM、真实多文档项目和 active chunk 完成 resource/project/chunk preview→export→Parser cold reopen→digest/count 对账。
3. ResourcePackage TMX shall 完成 managed snapshot export→cold validate→publication→receipt 对账；source resource 与 prior destination 在失败路径保持不变，TMX package import 负向拒绝。
4. Fault tests shall 覆盖 stale scope、detached/missing/foreign、unsupported metadata、locale、symlink/hardlink/special、parent replacement、stage/fsync/replace/readback/receipt/recovery。
5. Architecture guards shall 证明 Parser/Resource/Workspace/Chunk/ResourcePackage/TMX/Qt 的 owner 依赖方向未倒置。
