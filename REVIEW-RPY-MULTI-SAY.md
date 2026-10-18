# Ren’Py 同原文多译句：最小交付与架构评审

日期：2026-10-18。交接：`reassessment@88b089107ad46d1c3fc0a3fee746fbcf3a3812c4` 的 [HANDOFF-RPY-MULTI-SAY.md](HANDOFF-RPY-MULTI-SAY.md)；源码基线：`3b5979529ef739bc5968224b05489d3fc3cb4229`。

本轮只审阅、提出产品与合同建议，不修改实现、正式 R/D/T、ADR、审批或历史验收。已核对 handoff 示例、所列源码接缝及 Ren’Py/TMX 官方资料；未运行 LocalCAT、Ren’Py 游戏、Qt 旅程或新的候选验收。下文“源码确认”指静态控制流/数据结构，“建议”不是既有能力或实施授权。

## 1. 推荐结论：先编辑现有组，不同时交付脚本结构创作

**先支持已有多译句的导入、逐句编辑、整组确认、项目保存/冷重开和保真 TL 导出，足以成为有实际价值的第一步。追加、拆分、合并不必作为同一轮前置。** 用户已经在 TL 中写好了正文和玩家可见译注，当前首先需要解除“整个项目无法打开”，而不是由 LocalCAT 生成新的脚本播放结构。

但“先支持编辑”不能收缩为只放宽 scanner：有序组必须进入项目的真实编辑与持久化模型，源更新必须识别模板结构冲突，TM/TMX 必须明确文本转换损失。只留下一个合并的 target 字符串、把后几句藏在私有 payload 或 UI 缓存中，不算完成。

| 优先级 | 本轮应包含 | 本轮不包含 |
| --- | --- | --- |
| P0 | 一个原文对应一个有序目标组；原句数/顺序不变，逐句编辑全部正文和可见译注；原始模板不可用时仍可从包导出 | 追加、删除、拆分、合并、重排 say；修改 speaker/属性/控制行；完整 Ren’Py 脚本编辑器 |
| P0 | 源更新的组结构兼容检查；不能将旧组按序号塞入新的不兼容模板 | 自动逐句对齐、跨 label 合并、通用三方脚本 merge |
| P1，首版消费边界 | 旧单译句项目、strings、确认/撤销、搜索与校对；TM 查询/应用边界；TM/TMX 整组正文投影及损失提示 | TM 数据库增加脚本 AST、把多个 TUV 当有序 say、通过 TMX 承诺 TL 保真还原 |
| 后续独立范围 | 有明确创作需要后，先评估低频追加，再评估拆分/合并 | 为这些未来操作预建全局子句 UUID、控制流图、分支版本系统或通用编辑框架 |

“多译句”是同一翻译单元中连续播放的几个目标 say，不是 TM 里供用户择一的几个备选译法，也不是若干 source 相同但 label 不同的独立段落。后者继续保留各自身份，不因原文相同而合并。

## 2. 示例与源码核对

### 2.1 示例不是 strings 的重复 new

handoff 的 `gate_001` 是一个 dialogue block：一条原文注释，三条实际目标 say，中间有 `nvl clear` 和普通注释。玩家看得到的两个括号译注是 target 正文；`# Translation note...` 和行尾 `# Visible...` 才是脚本注释。

Ren’Py 官方翻译文档明确允许一个原始 dialogue 被翻译为多条 say；strings 的 old/new 则是单一替换。say 本身带交互行为，`nvl clear` 是执行控制，不是装饰排版。[R1][R2][R3] 因而不能把三条 say 改为一条带 `\n` 的字符串，也不能把译注转成 `#` 注释。

### 2.2 已确认的接缝

| 源码 | 静态事实 | 对本轮的含义 |
| --- | --- | --- |
| `parser_rpy_codec._Block/_Slot/scan_tl` [P] | 只保存一个 target；第二个 target 触发 ambiguous-slot；还强制源/目标 speaker 内容及形式相同。示例第 8 行符合该拒绝分支 | 不是仅调整计数；须定义一个 source 后的有序目标集合，并明确已有异 speaker 目标的支持边界 |
| `_Say`、`_private_slot`、`prepare_round_trip_bytes` [P] | 已能识别单个 say 的 speaker、属性、临时属性和 transition；私有映射只有一个 target_span；导出替换 literal 内部而非整行 | 复用词法和字节跨度替换，不必新增执行 AST；私有映射与输出编辑合同都要支持多个跨度 |
| `ProjectSegment`、`EditingOverlayEntry`、`WorkspaceSegmentView` [W][WS] | target 是单个字符串，确认和 dirty/digest 也围绕这个值 | 所有句子必须纳入实际编辑状态、dirty、保存与撤销，不能只在展示层拼出多句 |
| `ProjectPackage` [PK] | document-v1 为既有严格载体，源字节与 codec-private 可随包携带 | 保真素材已有归属；修改的是目标内容表示和兼容读写，不另造 RPY JSON 项目 |
| `QtEditorWindow._target_changed`、Controller 更新入口 [Q][C] | workspace 路径将单个 `target_editor.toPlainText()` 交给 `update_workspace_target`；另有 legacy 单文档路径 | 新组编辑要贯通 issued segment/当前子句、确认和消费，不能让 old scalar 命令默默覆盖整个组 |
| `ProjectWorkspaceService` 的分类和 `_build_candidate` [WS] | 同身份按 source_fingerprint 分 unchanged/source_changed；前者保留当前 overlay，后者保留 target 并撤销 confirmed；模板来自 incoming document | “原文未变”不等于 say 数量、属性或控制行未变；自动保留 overlay 必须增加映射兼容前提 |
| `rpy_text_rules.validate_target` [T] | 以 source 的全部 interpolation/tag Counter 校验一个 target，检查早于未修改 target 的原字节复用 | 不能对每个子句重复要求全部 source token，也不能把多个 say 的 tag 栈拼成一个文本块 |
| `parser_tmx_codec._LocaleChoice.observe` [X] | 同规范化 locale 的后一个候选覆盖前一个；inline XML 有独立拒绝策略 | 不以多个同语言 TUV 或未支持 inline XML 保存有序 say |
| TMX scope/materialization 与现有 Design [XC][XD] | workspace 单元直接投影 scalar target；writer profile 当前使用文本 seg、固定 sentence header、既有 loss policy | 多句正文投影、block 分段标识、loss 与 scope 复证须由原 TMX owner 修改 |

本轮没有重新执行 handoff 中的错误复现，也没有验证“修改后可通过”。上述静态事实足以判定需要哪些合同修改，无须先建一个大型可行性 spike。

## 3. 最小模型：一份原文、一个组、一份模板

### 3.1 数据所有权

建议将目标内容显式区分为 scalar 与 ordered group，或使用等价的有界中立表示。具体类型名由 owning Design 定义，以下是不变量，不要求新增通用模型框架：

- **项目 owner** 保存有序子句文本、父 segment 身份、整组 confirmed 与编辑版本。一个组仍是一个 Project segment；文档顺序、Chunk membership 和 source 身份不拆成 N 份。
- **RPY codec** 保存/重建每条目标 say 的 literal span、quote、只读 speaker/属性/transition 以及穿插原始字节的映射。控制行、注释、缩进、空行和行尾注释仍归模板，不成为 target 子句。
- **Parser** 提供有界中立输入/round-trip 编辑合同；不理解 `nvl clear` 的业务含义，不获得 Project 权威。
- **Qt/Controller** 消费中立组内容与只读展示信息，不自行解析 Ren’Py 或 codec-private。只读语法提示由已知 codec/adapter 投影；codec 不可用时至少保留全部中立文本和原始成员，不从私有字节猜测执行权限。

不要同时持久化“可独立修改的合并 target”和“可独立修改的 target_parts”。组的权威内容只有一份；兼容的文本展示或 TM 投影应单向派生。不能把 JSON 数组塞进 target 字符串，不能约定按换行拆分——一条 say 自己就可以包含逻辑换行。

### 3.2 身份、版本和旧项目

父段落继续按现有 Document＋language/label 对应；不同 label 下的同原文不是同一段。新 codec 版本不能仅因升级就把旧单译句段落全部变成 NEW/REMOVED。strings 的现有身份和 old/new 语义保持。

首版固定句数和顺序，子句定位可用“父 segment＋当前模板版本内的 slot key/序号”，并在编辑提交时核对当前项目/模板代次。**不要求现在生成跨模板稳定 UUID。** 序号仅是当前模板内的地址，不能在源更新时证明新旧第 2 句是同一对象；未来真支持结构操作时再按实际需要升级。

旧 scalar 文档由 Project owner 读取为等价单句状态，不能根据旧 target 中的换行推断组。新模型读写必须有显式兼容判别：需要变化的 document schema/编辑表示和 codec-private 版本分别处理，不能在 strict v1 中悄悄多塞字段；外层 ZIP carrier、发布/恢复状态机无实际变化时不必整体重造。旧版本客户端不能理解新组时明确拒绝或只读，绝不能只保留第一句后保存。

旧单句 private payload 要有明确的读取/重建兼容路径；新组的跨度集合、条数和 codec 身份必须共同核验。原 source/private digest 在这里用于绑定实际模板字节，不需要新增源码快照台账或全仓库重签机制。

总限额须覆盖实际子句数、全部编辑文本和私有映射，而不只计算父段落数。复用现有输入/字符串/包限额，并给总 say 数设有界预算，避免“一个 segment 内无限个 target”绕过 max_records。

## 4. 保真、注释和保护文本

### 4.1 固定结构的可观察保证

首版应保证：无译文修改时字节相同；编辑某句时只替换该 literal 内部；其余目标、source 注释、speaker、属性、transition、voice/nvl、普通及行尾注释、BOM/换行/缩进均保持。编辑空字符串不删除 say，空目标不回退原文。

已有异 speaker、属性或 transition 的目标，可以在现有 `_say` 有限词法可识别时纳入**只读结构、仅正文编辑**。这需要明确放宽当前 source/target speaker equality，不需要开放修改 speaker。无法识别的调用表达式、条件/Python 块仍按声明范围拒绝，不因 Ren’Py 引擎支持就自动纳入。

注释不能只靠“有引号”认成新原文。建议明确有限规则：本 profile 的 dialogue source 是首次实际 target say 前唯一符合声明形式的源注释；多个前置候选或缺失 source 仍拒绝。首次 target 之后的注释保留为模板注释，不继续猜测另一组配对。该规则应写进 profile 并覆盖带引号的译者备注、注释掉的旧译句和 `# guide "..."`；不暗中推广为任意多 source block 支持。

### 4.2 保护检查需要调整，不能逐句复制旧校验

例如 source 包含 `[name]`，目标第一句使用 `[name]`，第二句只有译注。第二句没有 `[name]` 不是遗漏；把 `validate_target(source, child)` 调 N 次一定过强。反过来，简单 join 后运行一次也不能检验 tag 是否跨了真实 say 的结束边界。[T]

建议 owning Design 明确三层检查：

1. 字符串编码、转义、interpolation 词法与格式边界逐子句检查；不执行表达式。配对 tag 的栈逐实际 say 重置，不能靠下一句的闭合 tag 修补上一句；保留 Ren’Py 允许文本块末尾隐式结束样式的规则，不额外要求所有样式显式闭合。[R4]
2. 对 source 中需保留的 interpolation，在**整个目标组**核对所需 token，而不是要求每句都有；已分散的 token 不复制。`nvl clear`、voice 和原有时序类文本控制的所属 say/间隙先保持，不以组内总数相等允许跨控制边界移动。
3. 区分输入已存在的差异与本次编辑引入的错误。当前 writer 在 no-op 之前执行 source/target token 校验，可能使能读入的既有目标无法零改动导出。[P][T] 建议完整模板与映射已验证且目标全未修改时保留原字节并提示已有差异；对修改后的组，新增丢失/改写/未知 interpolation 默认阻断发布，补齐 source 已声明项应允许；已有不一致不应因普通正文编辑反复被当成新损坏。已有逐句样式及为多句分别闭合的样式应按目标边界判断，不机械要求 tag 总数等于单个 source。

第三点是对现行硬校验适用范围的**明确合同调整建议**，不是声称代码已经支持，不能直接删测试实现。应在有限样例上固定规则；source 更新后的新增占位符仍必须进入复核，不能借“原来如此”自动确认。草稿可以保存，无法安全导出的修改应在预览定位到具体子句，不能丢弃译者的草稿来满足校验。

本轮不承诺验证游戏自定义 tag、角色回调或实际播放效果；保留脚本字节与声明子集的编辑安全，不等于执行过游戏验收。

## 5. 校对、确认和撤销

推荐左侧显示一份原文，右侧按播放顺序纵向显示全部目标子句；每句有序号和可辨认的只读说话人。`nvl clear`/voice 在实际间隙显示轻量只读提示，普通注释可展开查看，玩家可见译注像正文一样直接编辑。长组可折叠，但当前编辑句、搜索命中和诊断句应自动展开；不使用默认只显示一条的轮播，否则容易漏审译注与前后衔接。

首版以整组 confirmed 为唯一确认事实；任一子句修改即撤销该组确认。单句“看过了”的细分进度不是本轮必要模型。含空子句允许保存和原样导出并提示数量，不能仅因第一句非空就显示整组完成；如用户明确确认含空句的组，应保留其决定，不把空 say 等同于删除。自动 translated 统计可要求全部子句非空，避免把部分完成算成完整；不扩展一套新的全局状态系统。

沿用既有快捷键含义：确认针对整组，句内 Enter 仍是该句文字换行，不偷偷执行拆分。子句导航与段落导航应区分；切换焦点前提交正确子句，撤销恢复该次编辑、整组确认状态及相关 dirty。UI 本地 QTextEdit undo 不足以证明源更新/确认/跨句操作都可撤销，后两类必须交给原 Project/Controller edit owner。

查询仍由父 source 驱动，不为每条译注重复查同一原文。搜索和浏览要覆盖全部目标子句，命中定位到父段＋当前子句＋句内 offset；不能在拼接字符串上得到跨 say 的伪命中或把 offset 回填第一句。批量替换/预处理要么逐子句执行并整组提交/撤销，要么对暂不支持的组显式拒绝；不能静默退回 scalar setter。

所有组内修改仍受既有 project/session/revision、只读/Chunk 权限及过期建议检查，不新增第二个编辑 authority。组内句子不改变 Chunk membership。

## 6. 源更新：保留译文必须有模板兼容前提

现有 unchanged/source_changed 分类可以复用，但 source_fingerprint 不足以授权 target 的结构映射。[WS] 建议分别考虑源文变化、目标槽结构变化和无关模板字节变化：

| 新输入与当前模板的差异 | 最小行为 |
| --- | --- |
| 原文/槽结构均未变 | 保留当前整组编辑与确认；无关模板字节可按原流程更新 |
| 同 label 原文改变，目标槽结构兼容 | 保留全部子句文本，整组撤销确认；用新 source 重新进行导出保护检查，不能只保留第一句 |
| say 数量/顺序、speaker 形式、属性/transition、voice/nvl 相对位置改变 | 明示结构冲突；不按序号、文本相似度或相同句数自动套用旧组 |
| 只改变 BOM、换行、普通/行尾注释或安全空白 | 映射重建后可采用新模板，不应仅因整文件 SHA 变化就丢弃译文或撤销全部确认 |
| label 改变、重复或无法唯一关联 | 继续使用既有 NEW/REMOVED/AMBIGUOUS/UNRESOLVED 与显式关联规则；不以相同 source 自动合并身份 |
| 外部 TL 只修改目标文本，本地也有编辑 | 在预览区分外部 target 变化；默认保留项目 overlay 的既有语义，不能把“source unchanged”显示成“所有内容无差异” |

首版不需要实现结构冲突合并器：**可阻止受影响文档的本次源更新，保留旧项目/旧模板/全部编辑，并展示新输入的差异与外部对齐后重试指引。** 不得提交新 source/private 模板却保留无法映射的旧组。也不能声称“保留 detached”自然保留了它原来的 codec 映射；如使用 detached，必须确认包内旧模板归属，否则采用上述更小的拒绝策略。

这意味着本轮可以处理结构兼容的正常源文更新，但不承诺自动吸收外部追加/拆分/合并。将三句项目对接新生成的一句空模板，属于明确冲突，而不是第一句胜出。未来结构操作可放宽此边界，无须现在建立通用三方 merge。

## 7. TM/TMX：整组正文可交换，播放结构不假装可还原

### 7.1 TM 建议与写回

推荐首版不改 scorer、canonical schema 或 Fuzzy 准入：

- 查询使用父 source 和现有源侧 context/speaker，结果是参考译文，不把当前每个子句分别建成同 source 的竞争候选。
- 普通 scalar TM 建议应用到组时，提供明确的“应用到当前译句”语义，只改用户选中的子句，其他句子和控制行不动；没有明确子句目标就不应用。仍执行原 issued suggestion/代次检查。这个动作是文字替换，不声称把完整组自动还原。
- 整组确认与 TM 写回分开报告。需写回时，一次写入 source → 按序拼接的**完整组正文**，包括玩家可见译注，不是 N 条 source → 局部译句。可复用现有 Update 偏好，但确认动作须明确“整组正文写入 TM，不含脚本结构”，不能只写第一句或悄悄丢注释。
- 文本投影可用固定 LF 连接全部子句，字符串内 tag/interpolation 原样保留；这是单向的文本转换，不是可逆序列化。不能根据这段文本的换行数量自动拆回 say。已有 provenance 可标识 text-only 转换及子句数量，但不得存入可执行语法，也不要求新存储 schema。
- 尚未实现该转换的阶段，可以确认/保存项目并明确显示“组内容未写入 TM”，不能假装写回成功；这种临时开发状态不算本轮 TM 消费完成。单句既有行为保持。

`renpy_tm_compat.py` 当前只处理同 speaker 包裹的一个 JSON 字符串，不应扩成解释任意 Ren’Py 片段的执行/拆组入口。[RC]

### 7.2 TMX 的首选边界

建议一个父 source group 导出一个 TU，source TUV 一个 seg，target TUV 一个按序拼接的正文 seg。整组采用适当的 TU `segtype=block`（不要把其他单句全部改成 block）；沿用 XML 转义和 Level 1 文本，不伪装成多个同语言 TUV 或未支持的 inline XML。TMX 本身支持 block/paragraph 等分段类型，也不保证 TU 的顺序就是脚本执行顺序。[M1]

示例的正文投影应包含“我们在门边停下。”及两段玩家可见译注；不包含 `nvl clear`、`guide`、属性、transition、普通及行尾 `#` 注释。预览明确提示“正文保留，Ren’Py 播放结构不保留”，统计受影响组；包内 TL 或 ProjectPackage 才是本轮保真交接方式。

这是对 TMX 现有 required-metadata/loss policy 的显式 text-only 转换增量，必须批准后才能把特定结构损失作为可接受警示，而不是直接宽化所有 blocking loss。[XD][XR] 部分空子句要提示；全组正文为空沿既有 empty-target 规则排除，不让连接分隔符制造虚假的非空翻译单元。

第一步不要求可选结构 metadata。未来只有真的要由 TMX 还原 say 时，才需要版本化顺序、边界、speaker/控制映射与缺失策略；第三方可丢弃 prop，收到无结构的纯文本不能声称无损，也不能把缺少自定义 prop 当作普通 TMX 损坏。本轮保持 parser 对同 locale 多 TUV 的既有选择语义，不借多句功能改变一般 TMX 导入。

Project/Chunk TMX 按父 segment 身份和既有 scope 过滤，组不能被 Chunk 边界拆开。ResourcePackage 的 TMX export-only 边界、JSONL/CSV 资源事务及设备资格隔离不随本项放宽。

## 8. 追加、拆分、合并为什么可以暂缓

现有组编辑的直接收益是打开、校对并交付用户已经完成结构创作的 TL。首版不支持句数变化，意味着确需增加新译注时仍需外部编辑并重新导入/明确处理模板冲突；应诚实写入产品范围，而不是叫完整 Ren’Py 创作支持。

结构操作不是给 tuple 加减一项这么简单：[R2][R3]

| 操作 | 需要单独决定的语义 |
| --- | --- |
| 追加 | 新 say 放在 `nvl clear`/voice/注释之前还是之后？继承哪些 speaker/属性？不能默认复制 transition 或 voice |
| 拆分 | 光标不能切断 interpolation/tag；样式和时序标签如何分配？同 speaker 的两次 say 也可能增加一次用户交互 |
| 合并下一句 | 即使文本可拼接，也会减少一次 say/交互；speaker、属性、transition 不同或中间有控制行时不能冒充无损合并 |
| 全部合并 | 更容易抹掉注释位置和控制间隙，当前收益不足，不建议首版 |

后续结构编辑可以继续使用同一个父组与模板映射扩展，给操作明确插入/删除位置并以一次 Project edit 记录撤销；本轮无需为未来先引入全局稳定子句 ID。两个阶段可独立交付，但模型与边界现在应正确，而不是以后从换行文本中反推结构。

## 9. 必要 R/D/T 差异与少量完整任务

### 9.1 合同修改归属

| Owner | Requirements 差异 | Design/实现接缝 |
| --- | --- | --- |
| `rpy-project-codec` | R1.1–1.2 限定一源多目标及注释识别；R2.1–2.4 有序组/空句/身份；R3.1–3.3 组保存与旧包；R4.1–4.4 多跨度、保真与组级保护；R5.3 模板结构冲突；R6.1–6.2 校对及 TM 消费；R7.1 总子句预算 | `renpy-tl-v1` 的 scalar-only grammar/private 表示要显式演进并说明兼容；`_Block/_Slot`、parse、private payload、prepare 及 RPY adapter |
| Parser | 现有中立 record/read 与 Requirement 10 round-trip 边界允许有界目标组，不把 RPY 私有语法泄露到公共接口 | `ParsedSegment`/`RoundTripSegmentEdit` 等相关数据合同、surface 验证和 capability/版本兼容；不能只改 codec 私有字段 |
| Multi/Project | 现有 R3/4/13 的 intake、项目模型/包条款及源更新适用范围；所有子句都参与编辑/确认、保存、dirty 和兼容判别 | `ProjectSegment`、`EditingOverlayEntry`、view、内容摘要、ProjectPackage document decoder/encoder、reconciliation；Project owner 唯一持久权威 |
| Qt/Controller、Feature5 | 父组＋子句焦点、整组确认/撤销/搜索、TM 应用和整组正文写回；旧单句/只读/Chunk 权限保持 | `_target_changed`/`update_workspace_target`、确认和 adapter、只读布局提示；公共 scalar 命令不能静默丢组 |
| `tmx-context-interchange` | R3 deterministic payload、R4 loss、R6/7 project/chunk scope 与 R10 预览加入明确 text-only 多句转换 | scope materialization、TU segtype、正文投影、loss codes 和冷 reader 对账；不改为脚本重建 profile |
| 平台/发行 | 只增加实际受影响模块与候选消费验证，不新建文件系统或资格规则 | 已有 build/资源定位/发布保护复用；Windows 现有发行、Mac/Linux source 按当前支持范围验证 |

以 `rpy-project-codec/cross-spec-amendments.md` 的现有 owner 映射承载本轮增量，不复制完成状态。[A] 如具体中立模型选择真的改变 ADR 已冻结语义，再由治理 owner 提出精确修订；不因“支持多句”自动创建一个新治理系统。所有新设计/版本/loss 规则均需按现有流程批准，本 REVIEW 不代替批准。

### 9.2 四个完整任务，而不是一串局部组件 PASS

以下是建议任务分组，不占用或勾选现有 Tasks 编号。

**任务一：已有译句组可保存并保真往返。** 贯通中立组合同、RPY 扫描/映射、Project overlay/package 与导出。退出条件是 handoff 三句示例无修改字节相同，分别编辑三句后只改变对应 literal；源 TL 路径不存在时冷重开仍完整；旧单句包和 strings 正常，新组不被旧 reader 截断。控制/注释/异 speaker/属性以及保护文本规则在本任务闭合。

**任务二：组编辑与校对真正可用。** 接通纵向多句 UI、子句焦点、整组确认/撤销、dirty/统计、搜索定位，以及 TM 查询/应用/整组正文写回。退出条件是正文和译注都可用键盘编辑、确认，切段/撤销不丢其他句；TM 不产出 N 个局部伪译法，过期建议不写错句，确认和 TM 写回失败分开显示。只读/Chunk 权限保持。

**任务三：源更新不误配或丢弃组。** 贯通兼容结构下的源更新、只改注释/换行与目标模板变化的区别、结构冲突的可见拒绝。退出条件是同 label 改 source 保留全部目标且取消确认；3→1、同数但换 speaker/控制位置不会按序号套用；阻断时旧包与未保存编辑仍完整。首次不实现交互式结构 merge。

**任务四：正文互操作与实际交付旅程。** 由 TMX owner 完成明确的整组正文投影、block 分段、结构损失预览和冷重读；再以实际入口完成 TL→项目→编辑→保存冷开→TL，以及 ProjectPackage 跨设备与 TM/TMX 交换的差异说明。验证 export/edit/关闭竞争和旧格式回归，保留各平台证据范围，不因本文直接重签旧 frozen。若只修改 UI/Parser，不默认运行无关 100k Fuzzy benchmark；真实 Core 相关实现/输入变化按既有触发规则处理。

任务一先建立真实纵向数据链；任务二/三在其上收束不同消费者；任务四完成交换及产品出口。无需再建立独立“多句框架”里程碑。

## 10. 最小验证集与不确定性

建议六组针对性用例，不扩成全 Ren’Py 兼容矩阵：

1. handoff 三句＋`nvl clear`＋普通/行尾注释：零编辑、分别编辑、空子句、原模板缺失冷重开、TL 重读。
2. 混合单句/多句/strings、重复原文不同 label、已有异 speaker/属性/transition、带引号注释；不支持的条件/Python、多前置 source 注释继续定位拒绝。
3. `[name]` 分布在组内、tag 每 say 边界、隐式样式结束、转义换行与真实 say 边界不同；既有 token 差异与新增编辑错误分开；源更新新增 placeholder 不自动确认。
4. 旧包读取/保存、新 schema/codec 不兼容、缺失 codec 中立保全、超总子句/文本/映射限额和损坏 span/重复子句定位拒绝。
5. 同 label 源文变更、仅注释变化、3→1、同数但控制位置改变、外部 target 变化；确认/dirty/源更新取消与失败无丢失。
6. 校对与 TM/TMX：键盘焦点、组撤销、搜索第二/第三句、当前句应用、整组一次写回；一个 source TU/一个目标正文 seg、两段可见译注保留、结构损失提示、同语言多 TUV 不被误解为组，项目/Chunk scope 正确。

**当前没有必须先做的算法或引擎可行性实验。** 一源多 say 的存在由官方文档说明，scalar 接缝和映射风险由源码直接定位；实施后的真实纵向测试不是新的研究前置。若 Design 对已有子句引用能否稳定穿过包保存/源更新仍不确定，只需在任务一内用“一份三句模板＋一份结构兼容/不兼容更新”验证中立表示，发现必须解析 codec-private 或靠换行猜组就否决该表示；不扩成通用 AST、数据库迁移或脚本合并研究。游戏播放语义只在将来主动生成/移动结构时按确切新增语法做有限开发验证，不把安装 Ren’Py 加成 LocalCAT 使用前置。

尚待实际证明的是新模型兼容、Qt 校对体验、各转换策略和真实候选往返，而不是是否应先让用户编辑已经存在的三句。**首版完成标准是这些真实 TL 能完整进入、编辑、保存和交付；按钮数、子模型数或局部测试数量不是替代标准。**

## 参考来源

以下源码链接均固定到本轮基线；正式范围沿现有 owner 管理，本文链接不是新的运行时资格。

[P]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/parser_rpy_codec.py
[T]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/rpy_text_rules.py
[W]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/project_workspace_contracts.py
[WS]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/project_workspace.py
[PK]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/project_package.py
[C]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/editor_controller.py
[Q]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/qt_editor_window.py
[RC]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/renpy_tm_compat.py
[X]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/parser_tmx_codec.py
[XC]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/tmx_export_coordinator.py
[XD]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/.kiro/specs/tmx-context-interchange/design.md
[XR]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/.kiro/specs/tmx-context-interchange/requirements.md
[A]: https://github.com/BeFringe/LocalCAT/blob/3b5979529ef739bc5968224b05489d3fc3cb4229/.kiro/specs/rpy-project-codec/cross-spec-amendments.md
[R1]: https://www.renpy.org/doc/html/translation.html#more-complex-translations
[R2]: https://www.renpy.org/doc/html/dialogue.html#say-statement
[R3]: https://www.renpy.org/doc/html/nvl_mode.html
[R4]: https://www.renpy.org/doc/html/text.html
[M1]: https://www.ttt.org/oscarStandards/tmx/tmx14b.html
