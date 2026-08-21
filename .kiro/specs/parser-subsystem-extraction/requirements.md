# 需求文档

## 简介

本需求为 LocalCAT 建立用途明确、可验证、与编辑器和资源存储边界分离的单输入解析契约。它只定义一个输入如何被识别、验证、转换为有序中立记录，并如何报告诊断、能力和成功终态；它不定义多文档项目聚合、TM/术语存储事务、Qt 状态或跨设备同步。

主线保持为：UI increment → Parser rebaseline → multi-document → collaborative chunks → cross-device sync。Parser rebaseline 是契约和基础设施阶段，不提前实现后续 Feature。首波可见入口包括 LocalCAT JSON、source-only 逐行 TXT、PO/POT、TMX Level 1、normalized TM JSON，以及支持显式 source/target 列选择的 CSV/XLSX 术语资源。当前单 JSON 编辑路径和既有术语前两列入口必须通过兼容 preset 保持可用。

RPY 不属于 Parser 首波内建 runtime。`rpy-project-codec` 作为可配置的 format-codec plugin，在仓库边界形成 DDD 防腐层：它把外部 `.rpy` 表达转换为中立 parsed records，并独立拥有 RPY tokenization、sidecar、占位符保护、安全回填、导出和 round-trip 验证。Parser Foundation 只提供格式中立的 plugin port；LocalCAT Core 不导入 RPY 类型、不解释格式 token，也不直接写 `.rpy`。单个 translation script plugin 可在 Parser Foundation 后独立接入；只有 RPY folder 的多文件 Project 聚合、跨文件 source/target 配对和项目身份必须等待 multi-document。常规多文档项目通常由多个 TXT、JSON 或 XLSX 文件组成，并以一个文件对应一章；`CAT_Working_File.xlsx` 这类单 workbook 多 sheet、每个 sheet 对应一章的输入是特殊 origin。两者的聚合均属于 multi-document；术语列选择只决定一个 active sheet/CSV 中哪两列映射为 source/target，不建立 Project/Document，也不按项目语言自动猜列。

## 边界说明

- **范围内**：用途感知的格式选择；单输入中立文档和资源记录；LocalCAT JSON、source-only TXT、PO/POT、TMX Level 1、normalized TM JSON、CSV/XLSX 显式列选择及前两列兼容 preset；结构化验证和诊断；迭代器唯一成功终态；内容、编码、文件安全；读取、canonical write、source round-trip capability 的可观察声明；兼容入口归并到单一语法权威；与 Engine、Store、Qt、workspace、TM surface 的可观察边界。
- **范围外**：多文档 Project/Document/Segment 聚合、current-document 导航和过滤、文档分隔与进度；ProjectPackage、手工导入导出和 reconcile；协作 chunk、权限和冲突；自动同步、provider 和远端 broker；RPY plugin/ACL 实现与 RPY 多文件聚合；XLIFF；TMX context/provenance/export；canonical TM 存储、检索和 Gate D；speaker alias/profile、头像、推测名称和设备资格；根据项目语言自动推断术语列；Office/PDF/OCR；网络服务。
- **相邻期望**：`multi-document-project-workspace` 拥有项目级身份、文件集合、导航、进度、保存和调和；`rpy-project-codec` 以可配置 plugin/ACL 形式拥有 RPY 格式语义、token/sidecar 与可选回填/导出，并通过本规格的格式中立端口接入；`tmx-context-interchange` 拥有 TMX context、provenance 和 export；`tm-storage-retrieval-index` 与 Integration TM surface 拥有 TM 存储、检索、CONTEXT 精确语义、Gate D 100,000 条 TM 性能资格及 UI 投影；`speaker-display-profiles` 拥有后续展示 profile；`collaborative-job-chunks` 和 `cross-device-sync-plugin` 按主线后置。

### Scope Lineage（范围沿革）

- 本文在 `parser-subsystem-extraction` 原 Spec 身份下就地重生成，保留 `legacy_spec_id`，不建立同名第二权威。
- 已获项目 owner 批准的 `rebaseline-plan.md` 是本版 Requirements 的冻结输入；本文对其中的保留、重写、移出和主线顺序进行可验收展开。
- `parser-rebaseline` 只拥有本规格的 Parser 契约门。它不得修改相邻 Spec 的 brief、Requirements、Design、Tasks 或 review-clustering，也不得借本规格拥有 multi-document、RPY 聚合、chunk 或 sync 实施权。
- UI→Parser→multi-document→chunk→sync 的顺序是本轮治理边界；手工包→自动同步是后续演化方向，不改变本轮 Parser 的单输入职责。

## 术语

- **Effective_Purpose**：调用方声明的闭合输入用途。首波值为 `project_document`、`language_resource.translation_memory`、`language_resource.termbase`。
- **Format_ID**：稳定的逻辑格式标识；它与 Effective_Purpose 共同确定格式契约，不等同于文件扩展名。
- **Codec**：按已声明用途和格式能力读取、验证或写入输入的格式边界。
- **Parsed_Document**：一个输入文档的只读中立结果，包含来源引用、有序段落、文档 metadata、诊断和能力快照。
- **Parsed_Segment**：一个输入文档内的中立段落，包含局部 ID、source、可选 target/state、raw speaker 和格式 metadata。
- **Resource_Record**：供 TM 或术语 application service 消费的中立资源记录，不是 canonical TM/termbase 存储对象。
- **Parse_Issue**：具有稳定 code、severity、location 与不含正文的安全摘要的结构化诊断。
- **Terminal_Success**：一次解析迭代唯一允许下游提交 staged records 的成功终态。
- **Canonical_Write**：依据中立结果生成规范输出的写能力。
- **Source_Round_Trip_Write**：利用格式 token 保留源格式结构的写能力；它不同于 Canonical_Write。
- **Format_Codec_Plugin**：位于外部格式仓库边界的可配置 DDD 防腐层；它把外部表示映射为中立结果，并独立拥有格式私有语法、token/sidecar 与可选写回能力。
- **Raw_Speaker**：段落携带的 speaker 原文身份；不包含 alias、显式空白 profile、头像或推测名称。
- **Termbase_Column_Selection**：调用方为 CSV/XLSX 术语输入显式提供的 source/target 列映射和 header 处理策略；每列使用 header 名或零基物理列索引，前两列只是既有入口主动选择的兼容 preset。

## 需求

### 需求 1：用途感知的格式选择

**目标：** 作为调用 Parser 的应用服务，我希望先声明输入用途再选择格式，以便同一扩展名不会形成错误的数据权威。

#### 验收标准

1. When 调用方请求格式选择时，Parser shall 要求一个明确的 Effective_Purpose，以及一个 Format_ID 或有界的候选发现请求。
2. The 首波 Effective_Purpose shall 采用三个闭合值：`project_document`、`language_resource.translation_memory`、`language_resource.termbase`。
3. When 使用扩展名、MIME hint 或有界内容 sniff 时，Parser shall 只在调用方声明的用途内缩小候选，不得用 hint 替代用途声明。
4. When 两个 codec 声称同一 `(Effective_Purpose, Format_ID)` 权威时，注册结果 shall 确定性地拒绝重复，不得采用后注册覆盖前注册。
5. When 没有兼容 codec 时，选择结果 shall 说明请求用途、观察到的格式 hint 和可支持组合，不得解析或修改输入内容。
6. The 首波 `project_document` shall 包含 LocalCAT JSON、逐行 TXT 和 singular-profile PO/POT；能力分别遵循需求 3 与需求 4。
7. The 首波 `language_resource.translation_memory` shall 包含 TMX Level 1 和 normalized TM JSON。
8. The 首波 `language_resource.termbase` shall 支持 CSV/XLSX 显式 Termbase_Column_Selection，并提供由既有 Application 入口主动选择的“前两列 source/target”兼容 preset；Parser 不得根据项目语言自动猜列。
9. When 请求 PO/POT 作为 language resource、TMX 作为 project document、或请求合同外组合时，注册结果 shall 在读取记录前拒绝该组合。

### 需求 2：单输入中立结果

**目标：** 作为应用服务开发者，我希望 Parser 返回单输入的中立结果，以便编辑项目、语言资源、RPY 相邻 codec 和未来 workspace 在各自边界内消费它。

#### 验收标准

1. When project document 解析成功时，codec shall 返回一个 Parsed_Document，包含来源引用、Format_ID、有序 Parsed_Segments、文档 metadata、诊断和能力快照。
2. When language resource 解析成功时，codec shall 返回有序 Resource_Records，并明确这是资源输入，不得把它报告为项目文档。
3. The Parsed_Segment shall 保留一个局部 ID、source、可选 target、可选 translation state、Raw_Speaker 和格式 metadata；结果不得暴露上层编辑器或存储对象。
4. When 输出记录时，codec shall 保持输入顺序，并要求局部 ID 在该输入文档内唯一。
5. When 源格式区分 target 缺失与显式空字符串时，结果 shall 保留这两种状态的区别。
6. When 源格式没有持久化确认状态时，结果 shall 报告缺失或格式派生状态，不得凭空报告 `confirmed=true`。
7. The Parsed_Document shall 只代表一个输入，不得分配项目级 document ID、跨文档 segment ID、项目排序、dirty、reconcile 或聚合保存结果。
8. The Parser Foundation shall 提供格式中立的 plugin port，暴露 parsed records、结构化诊断、opaque capability 与 terminal outcome；`rpy-project-codec` 等 Format_Codec_Plugin 的格式 token、sidecar 与写回语义不得进入 Parser 或 LocalCAT Core。

### 需求 3：LocalCAT JSON 与 TXT 兼容

**目标：** 作为现有桌面编辑器用户，我希望重基线不改变已保存的单 JSON 项目和 TXT 打开行为。

#### 验收标准

1. When LocalCAT JSON 根为数组时，codec shall 使用文件 stem 作为文档名，并使用 `en-US` 和 `zh-CN` 作为缺省 source/target locale。
2. When LocalCAT JSON 根为对象时，codec shall 要求 `segments` 数组，并对缺失或空的 name、source_locale、target_locale 使用当前兼容路径的缺省值。
3. When 接受 JSON segment 时，codec shall 要求非空字符串 source，接受字符串或 null 的 target、speaker，在 confirmed 出现时要求布尔值，并仅在 ID 缺失或为空时生成 `segment-{1-based index}`。
4. When 读取 JSON 字符串字段时，codec shall 按当前单项目路径去除首尾空白，保留内部字符和顺序。
5. When JSON segment 不是对象、字段类型无效、source 为空或局部 ID 重复时，codec shall 使整个项目解析失败，不返回可提交文档。
6. When 接受 UTF-8 或 UTF-8-BOM TXT 时，codec shall 按输入顺序为每个非空 trimmed line 建立一个 source-only segment，按非空行的稠密顺序生成连续局部 ID，并使用文件 stem 作为文档名；target shall 为 missing，translation state shall 缺失，Raw_Speaker shall 为空身份，兼容 Application shall 映射为空 target 且 `confirmed=false`。
7. When JSON 或 TXT 没有可翻译 segment 时，解析 shall 以结构化 fatal issue 失败。
8. When LocalCAT 项目执行 Canonical_Write 时，输出 shall 是 schema version 1 的 UTF-8 JSON，包含 name、locales 以及按顺序保存的 `id/source/target/speaker/confirmed` 字段。
9. When canonical write 在原子替换完成前失败时，既有目标字节 shall 保持不变，且不得返回成功 receipt。
10. The TXT 首波 capability shall 为只读，不得宣称 TXT writer 或 source-preserving round trip。

### 需求 4：PO/POT 单数项目读取

**目标：** 作为 gettext 项目用户，我希望可靠读取 PO/POT 的单数翻译单元，不吞掉语法错误或悄悄折叠 plural 变体。

#### 验收标准

1. When 读取有效的 singular PO entry 时，codec shall 将解码后的 msgid 作为 source、msgstr 作为当前 target，将可选 msgctxt 作为格式 metadata，并产生由输入 entry 确定性派生的局部 ID。
2. When 读取有效 POT entry 或未翻译 PO entry 时，codec shall 暴露显式空 target，不得标记 confirmed。
3. When gettext quoted string 跨行或含有效转义时，codec shall 使用统一语法规则完成拼接和解码。
4. When header entry 的 msgid 为空时，codec shall 将 header metadata 保留在文档级，不得作为可翻译 segment 输出。
5. When entry 含 comments、references、flags 或 previous-value comments 时，codec shall 将其作为不透明格式 metadata 保留，不得解释为 speaker 或 TM context。
6. When entry 标记 fuzzy 时，codec shall 保留 target，并以格式派生的未确认状态暴露。
7. When 首波 singular profile 遇到 plural entry 时，解析 shall 以结构化 unsupported-capability issue 失败，不得折叠或丢弃 plural 变体。
8. When PO/POT 语法、编码或转义无效时，codec shall 返回带可用行位置的 fatal issue，不得返回部分成功。
9. The 首波 PO/POT capability shall 为 project document 只读，不得宣称 language resource、canonical write 或 source round-trip write。

### 需求 5：语言资源兼容与特殊 workbook 边界

**目标：** 作为语言资源管理员，我希望保留当前兼容的 TMX、normalized TM JSON、CSV/XLSX 读取语义，同时不把多 sheet workbook 偷换成项目解析。

#### 验收标准

1. When 读取符合首波 TMX Level 1 profile 的输入时，codec shall 先按规范化 locale 精确选择 source/target，再使用无歧义的 base-language fallback。
2. When TMX translation unit 含 inline XML、缺少可用 locale pair、base-language fallback 有歧义或超过 codec limit profile 的 segment text limit 时，codec shall 发出 record warning 并跳过该 unit，继续处理同一份语法有效文件。
3. When TMX 没有 translation unit、XML 无效、包含 DTD/ENTITY 或触发输入级限制时，解析 shall 以 fatal issue 结束，不得授权资源 commit。
4. When normalized TM JSON 输入有效时，codec shall 要求数组根，接受 source/target 为非空字符串的对象记录，保留字符串 speaker 为 Raw_Speaker，并为拒绝行发出 record warning。
5. When CSV 或 XLSX 术语资源输入有效时，codec shall 只消费 Termbase_Column_Selection 指定的 source/target 两列，跳过空行或不完整行并报告结构化计数，保持接受行顺序。
6. When XLSX 含多个 worksheet 时，首波 codec shall 只读取 active worksheet 并报告该 capability；不得隐式聚合多个 sheet。
7. When 输入是 `CAT_Working_File.xlsx` 或其他多 sheet workbook 时，Sheet 到 Project/Document 的聚合 shall 等待 multi-document Spec，不得由本需求产生项目身份、File_ID/Location 语义或 source/target 语言列识别。
8. When 资源中出现重复 source 时，codec shall 保留顺序和重复事实；去重、覆盖、canonical identity、activation、indexing 和 commit shall 由消费资源用例拥有。
9. When 任何 language resource 解析完成时，结果 shall 不写 JSONL、CSV、SQLite、resource manifest 或 live canonical store。
10. The 首波 TMX profile shall 不拥有 CONTEXT、provenance、TMX export、inline-tag round trip 或 `101%` 数字约定；CONTEXT 精确语义继续由 Integration TM surface 拥有。
11. When Termbase_Column_Selection 按 header 名选择时，codec shall 将首个物理行作为 header，只在非空字符串 header 中以去除首尾空白后的精确、大小写敏感文本匹配列名；缺失、重复命中、source/target 选择同一物理列或所选 header 无效 shall 在输出记录前失败。
12. When Termbase_Column_Selection 按零基物理索引选择时，codec shall 要求两个非负且不同的索引，并要求调用方显式声明无 header、首行为 header 或既有 header allowlist 兼容策略；任一数据行缺少所选列时 shall 按 record warning/skip 处理，不得回退到其他列。
13. When 既有术语导入入口未向用户暴露列选择时，Application facade shall 显式传入前两列兼容 preset；Parser codec 本身不得把 selection 缺失解释为隐式默认。

### 需求 6：结构化验证与诊断

**目标：** 作为用户和调用方，我希望解析失败有稳定、可定位且不泄露正文的说明，以便在不猜测的情况下修正输入。

#### 验收标准

1. When 请求 validation 时，codec shall 返回包含 outcome、Format_ID、观察到的 capability、issue counts 和有界 Parse_Issues 的结构化报告，而非只有布尔值。
2. The Parse_Issue shall 包含稳定 code、warning 或 fatal severity、可用的 byte/line/record location，以及不嵌入 source/target 正文的安全摘要。
3. When validation 报告成功时，该报告 shall 只表示被验证的 snapshot 满足同一语法和 limit profile，不得保证之后被改变的文件仍可解析。
4. When validation 与 parse 之间的输入 snapshot 改变时，parse shall 重新验证新 snapshot 或在返回记录前以 stale 失败。
5. When issue retention 达到 codec limit profile 的保留上限时，结果 shall 保留按 code 汇总的确定性计数，并报告截断，不得分配无界诊断正文。
6. When 输入路径不存在、不是 regular file、无法读取或用途/格式不受支持时，操作 shall 在输出可提交记录前失败。

### 需求 7：迭代器终态与提交授权

**目标：** 作为消费大量记录的应用服务，我希望只有唯一成功终态才可提交，以便尾部错误、取消或提前停止不会留下部分导入。

#### 验收标准

1. When codec 暴露 record iteration 时，每个已发出记录 shall 在观察到 Terminal_Success 前保持 provisional。
2. When 解析因 fatal syntax、limit violation、取消、consumer exception、early close 或缺少终态结束时，操作 shall 不授权任何已发出记录 commit。
3. When 观察到 Terminal_Success 时，终态 shall 绑定输入 snapshot identity、输出记录数、warning counts、codec identity/version 和所用 limit profile。
4. When consumer 导入 resource records 时，consumer shall 在 Terminal_Success 后才提交 staged effects。
5. When batch 含多个输入文件时，每个文件结果 shall 独立报告，不得互相修改或重新标记。
6. When batch 中某文件失败时，batch shall 只按调用方明确选择的 continue/stop policy 行动，不得把该文件解释为部分成功。
7. When 在限制范围内同时支持 materialized 和 iterator view 时，两者 shall 对同一 snapshot 产生相同的接受记录顺序、issue 和终态计数。

### 需求 8：可观察资源限制与 Fail-Closed

**目标：** 作为调用 Parser 的应用服务，我希望每个 codec 使用有限、版本化且可观察的资源限制，以便异常输入不会产生无界消耗或部分成功。

#### 验收标准

1. The 每个 codec shall 发布有限、版本化且可观察的 limit profile，覆盖该格式适用的输入、字段、记录、materialization、诊断、结构深度与展开资源维度。
2. When cancellation request 被观察到时，codec shall 在下一个有界取消点停止，并以无 Terminal_Success 结束。
3. When codec 声明的 limit 被超过时，结果 shall 使用稳定 limit code，不得降级为 warning，也不得返回可提交文档或资源集合。
4. The validation 和 terminal report shall 可观察地携带 limit profile 与 codec version，使未来变化不能静默改变输入接受语义。
5. The 当前 resource importer 的 100 MiB 输入上限与 TMX 单 segment 1,000,000 字符上限 shall 只作为该既有资源路径的兼容事实保留；它们不得被外推为所有 Parser 输入或所有字段的通用上限。
6. The Gate D 既有 100,000 条 TM 查询、迁移与内存性能资格边界 shall 继续由 `tm-storage-retrieval-index` 拥有；Parser shall 不改写该边界，也不得将其解释为 Parser 记录数量上限。

### 需求 9：内容、编码与文件安全

**目标：** 作为翻译内容的所有者，我希望 Parser 不猜测、替换或提前转义文本，以便格式迁移不会悄悄改写数据。

#### 验收标准

1. When 所选格式的文本编码无效时，codec shall 返回结构化 encoding issue，不得使用替换字符或 best-effort detection。
2. When 接受内容时，codec shall 保留解码后的 source 和 target 内部字符，仅允许格式明确要求的解码，以及以下已记录的首尾空白兼容：LocalCAT JSON 字符串字段、TXT 非空行、TMX `seg` 文本、normalized TM JSON 的 source/target/speaker、CSV/XLSX 所选 source/target 单元格与 header；除此之外不得执行未声明的 trim 或规范化。
3. The codec shall 不进行 HTML、SQL、shell escape，不做 Unicode normalize、case-fold、翻译推断或返回前的换行改写。
4. When 解析 XML 时，外部 entity、DTD resolution 和 network access shall 保持关闭。
5. When 解析 XLSX 时，macro、formula、external link 和 embedded object shall 不执行；data-only cell value shall 作为不执行的输入处理。
6. When source reference 指向非 regular file 或越过调用方提供的安全 root 时，操作 shall 在消费内容前失败。
7. The Parser subsystem shall 不执行 network access，并保持已接受内容在本地处理。

### 需求 10：能力声明与写入边界

**目标：** 作为上层功能开发者，我希望每个 codec 诚实声明可读、可规范写或可保留源格式写，以便 UI 和应用不会显示不可用操作。

#### 验收标准

1. When 选择 codec 时，调用方 shall 收到不可变 capability snapshot，至少说明 reader、validator、Canonical_Write、Source_Round_Trip_Write、streaming 和 format profile。
2. When 请求的 capability 不存在时，操作 shall 在打开目标或修改状态前失败。
3. The reader 的存在 shall 不得推导出任何 writer capability。
4. When 宣称 Source_Round_Trip_Write 时，codec shall 将 token 绑定到 codec identity/version、输入 snapshot fingerprint 和相关格式状态。
5. When round-trip token 缺失、外来、过期或版本不兼容时，写入 shall 在第一次修改目标前失败。
6. When 只宣称 Canonical_Write 时，输出 shall 明确是规范转换，不得声称保留源字节或排版。
7. The 首波只有 LocalCAT JSON 声明 Canonical_Write；TXT、PO/POT、TMX、normalized TM JSON、CSV 与 XLSX 均为 reader-only，不得因存在 reader 而自动获得 writer 或 round-trip capability。

### 需求 11：Parser 与应用层边界

**目标：** 作为系统架构师，我希望添加格式不要求修改匹配引擎或存储，以便各组件保持独立演化。

#### 验收标准

1. The Parser contract shall 不要求 Engine、Matcher、canonical TM record、TMStore、SQLite、Qt、Controller、workspace state 或 sync-provider 类型才能消费中立结果。
2. When 打开 project document 时，应用层 shall 将 Parsed_Document 映射到现有编辑项目行为，但不得让 Parser 成为 project session authority。
3. When 导入 language resource 时，资源 application service shall 拥有去重、合并、staging、durability、receipt、activation 和 rollback。
4. When 新 codec 满足 Effective_Purpose/Format capability contract 时，Engine 和 storage 行为 shall 保持不变。
5. The Parser subsystem shall 不查询 TM match、不计算 EXACT/CONTEXT/FUZZY similarity、不建立 speaker profile、不更新 termbase/TM resource。
6. The `BaseParser` 名称如需兼容可继续作为 facade，但正式一致性 shall 由行为和 capability contract 决定，不由名义继承决定。
7. When Format_Codec_Plugin 缺失、禁用或版本不兼容时，应用 shall 返回结构化 unsupported-capability 结果；LocalCAT Core shall 不启用内建格式 fallback。
8. The LocalCAT Core shall 只消费中立 parsed records、opaque capability 与结构化结果，不得导入 RPY 类型、解释 RPY token/sidecar 或直接写入 `.rpy`。

### 需求 12：Raw speaker 保留与非推断

**目标：** 作为带说话人字段的项目用户，我希望迁移保留同一 raw speaker 身份，同时不擅自创建展示 profile。

#### 验收标准

1. When 支持的格式提供 speaker 字段时，Parsed_Segment 或 Resource_Record shall 将其作为独立 Raw_Speaker 字段携带，不得拼接到 source。
2. When LocalCAT JSON 或 normalized TM JSON 的 speaker 缺失或为 null 时，Raw_Speaker shall 使用当前兼容路径约定的空身份。
3. When 提供的 speaker 不是字符串时，记录或文档 shall 按格式声明的验证策略失败，不得把该值强制转成字符串。
4. When 接受 Raw_Speaker 时，首尾空白 shall 遵循格式兼容规则，内部字符 shall 保留，身份比较 shall 保持大小写敏感。
5. The Parser shall 不创建 speaker alias、explicit-empty profile、avatar path、inferred name、device inventory 或 Fuzzy device qualification metadata。

### 需求 13：单文档与多文档 workspace 边界

**目标：** 作为未来多文档功能的设计者，我希望 Parser 只提供稳定的单输入局部身份，以便 workspace 独立拥有项目级身份、导航和调和。

#### 验收标准

1. The Parser shall 将每个 local segment ID 限定在一个 Parsed_Document 内，不得声称其为全局或项目级唯一。
2. When multi-document workspace 消费 Parsed_Document 时，workspace shall 拥有 document_id、display name/order、`(document_id, local_segment_id)` identity、current-document 导航、progress aggregation、dirty aggregation、reconciliation 和 save reporting。
3. When 当前单 JSON editor 消费 Parsed_Document 时，现有 one-project/one-document 行为 shall 继续可用，不得要求 workspace 先行。
4. The Parser shall 不打开 folder 作为 project、不选择 current document、不将全项目列表过滤成文档列表、不绘制文档分隔、不计算翻译进度、不写 ProjectPackage manifest。
5. When 项目由多个 TXT、JSON、XLSX 等文件组成，或单个 workbook 含多个 chapter sheet 时，自动聚合为一个 project shall 由 multi-document Requirements/Design contract 定义；常规一文件一章和特殊一 Sheet 一章均不得由 Parser 自行建立项目权威。
6. The Parser shall 不定义 chunk membership、权限、package synchronization、remote conflict policy 或 provider behavior。

### 需求 14：兼容入口与单一语法权威

**目标：** 作为现有脚本和桌面入口的维护者，我希望迁移期行为可回归且不会保留两份会分叉的解析实现。

#### 验收标准

1. When 现有编辑器打开或保存 LocalCAT JSON/TXT 时，可观察的成功、失败原子性、段落顺序、字段缺省和保存 schema shall 符合需求 3。
2. When 现有 TMX importer、术语 importer、normalized TM JSON CLI、PO handler、runner 或 glossary loader 在迁移期保留时，每个入口 shall 委托唯一 codec 语法权威，或明确标为本轮范围外。
3. When compatibility facade 委托新权威时，入口 shall 在版本化兼容合同明确移除前保持既有调用方返回形状和稳定失败映射。
4. When 某格式完成委托时，生产代码 shall 不得保留另一份独立实现其 tokenization、unescaping、validation 或 row selection 规则的 parser。
5. When legacy 行为与本需求冲突时，后续 Design migration table shall 记录冲突，不得静默保留平行权威。
6. The 当前 Qt editor、Excel 三态 runner、TM retrieval、术语 CRUD、workspace preferences 和 Feature 5 qualification 行为 shall 在 Parser migration 期间保持回归保护；本需求不得改变它们的语义 owner。

### 需求 15：验收证据与明确延期

**目标：** 作为项目 owner，我希望 Parser 契约由跨格式、失败和边界证据验收，并且不把未来 Feature 混入本轮。

#### 验收标准

1. When 后续实现本需求时，golden fixtures shall 覆盖每个 Requirements 定义的 Effective_Purpose/Format 组合的有效输入、格式边界、编码失败、限制和取消；TMX、normalized TM JSON、CSV/XLSX shall 另覆盖合同已定义的 record warning，可构造尾部错误的格式 shall 覆盖 fatal tail，registry 重复拒绝 shall 由 registry 级 fixture 覆盖。
2. When 同时支持 iterator 和 materialized view 时，property 或 metamorphic tests shall 在 materialization 限制内证明顺序、结果、issue 和终态计数等价。
3. When 注入 fatal tail、early close、consumer exception、stale snapshot、writer failure 或 resource commit failure 时，测试 shall 证明没有 partial success 或未授权目标修改。
4. When 执行架构一致性验收时，证据 shall 证明 Parser 与 Engine 互不依赖，且 Parser 不拥有 Qt、workspace、TMStore、SQLite 或 provider authority。
5. When 执行兼容性验收时，现有 JSON/TXT、TMX、CSV/XLSX、normalized TM JSON、Qt、runner、资源和 acceptance suites shall 保持通过，或具有明确的版本化契约变更。
6. The following capabilities shall 保持延期：multi-document aggregation/UI、ProjectPackage/import reconciliation、RPY plugin/ACL 实现与多文件聚合、XLIFF、TMX context/provenance/export、canonical TM storage/retrieval、speaker profiles、按项目语言自动推断术语列、Office/PDF/OCR、collaboration chunks 和 cross-device sync。
7. When 提议任何延期 capability 时，提议 shall 进入其 owning Spec，不得在 Parser runtime 阶段扩大本文。
