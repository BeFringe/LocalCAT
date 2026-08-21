# Parser Subsystem 重新基线调研

## 结论

Feature 5 完成后的实际代码没有一个成熟、统一的 Parser 子系统；格式语法仍分散在项目 facade、资源导入、CLI 与两个 Engine 内部 loader。新版 `requirements.md` 以当前代码/测试、Feature 5 后置 Steering、TM storage 边界和 multi-document 相邻合同为正面输入，并按 `rebaseline-plan.md` 就地重生成。目录内 Git 历史中的旧 Design/Tasks 只用于历史追踪，不能为新版架构提供正面论据或直接实施。

Parser Foundation 保持 codec/contracts 不导入具体 Engine、Store 或 UI 类型，Engine/Store 不导入 Parser，由 Application facade/adapter 同时消费两侧并完成映射和事务。codec 准入权威是用途、行为与 capability contract；当前仓库不存在生产 `BaseParser`，Design 不为历史草案凭空创建 compatibility facade。ADR-015 精确取代 ADR-004/005。

## 权威顺序

1. 用户当前明确决策；
2. `.kiro/steering/` 的项目级架构与技术边界；
3. 当前分支代码和测试，作为 **as-is 行为事实**；
4. 按 `spec.json` 标记为 approved 的 Parser Requirements/Design/Tasks，作为 **to-be 实施权威**；
5. README 仅作派生路线图与状态摘要；
6. 本目录遗留 `design.md`、`tasks.md` 与 Git 历史中的旧 Requirements 只保留历史意图。

在新版规格获批前，代码/测试优先于旧 Parser 草案；README 也不能覆盖代码事实。新版规格获批后，它定义未来目标，实施结束再同步 README。

## 当前解析主链盘点

| 用途 | 当前入口 | 当前事实 | 重新基线处置 |
|------|----------|----------|--------------|
| 编辑项目 | `editor_project.py` | JSON 对象/数组与逐行 TXT；统一保存版本化 JSON | 迁移为 Project Document codec |
| TMX 资源 | `resource_importer.py` | Level 1、locale 选择、DTD/ENTITY 拒绝、原子合并已完成 | 作为已实现基线迁移，不重新发明 |
| 术语资源 | `resource_importer.py`, `GlossaryLoader` | CSV/XLSX 与 CSV loader 重复 | 迁移后收敛为唯一实现 |
| 归一化 TM JSON | `tm_json_importer.py` | source/target 数组导入 | 注册为 TM Resource 用途，不与项目 JSON 冲突 |
| PO/POT | `tm_engine.POHandler` | 只读取源单元，`msgstr` 未进入契约 | 迁移为 project-document 单数 profile；不注册为 language resource |
| Ren'Py | 外部辅助工具 | 只可靠处理 Ren'Py 生成的翻译脚本 | 独立 `rpy-project-codec` plugin/ACL 规格；Parser 只供应中立 port |

## 当前架构缺口

- 当前不存在可供新增格式实现的中立 codec contract 或用途感知 registry；JSON 同时用于项目与 normalized TM，XLSX 同时存在术语资源和未来项目 workbook，用扩展名无法确定 authority。
- `editor_project.py` 直接产出 `EditorProject`，`resource_importer.py` 同时拥有 XML/表格语法与 canonical/legacy/store 编排；格式语法和 Application 事务尚未分开。
- `tm_engine.POHandler` 与 `glossary_engine.GlossaryLoader` 把格式 tokenization 放在 Engine 内；Feature 5 Core 的依赖守卫只能阻止新反向 import，不能自动移除这些嵌入式历史职责。
- TMX/termbase 解析与 normalized TM JSON CLI 分别拥有不同的 warning、skip、LWW 和写入语义；缺少统一的结构化 issue、snapshot 与 terminal outcome。
- 当前只有 LocalCAT JSON 的规范保存路径具有已验证原子替换；TXT、PO/POT、TMX、normalized TM JSON、CSV/XLSX 均不能由“存在 reader”推导 writer 或 source-round-trip 能力。
- Application 必须继续拥有 project-session mapping、resource 去重/activation/staging/commit/receipt；Parser Foundation 只产生一个输入的中立结果，不把这些现有 Application 职责重新包装成 `TMImporter`。

## MateCat 格式调查

MateCat 在线产品支持的格式由独立 Filters 服务转换为 XLIFF；公开 `MateCat-Filters` 已归档，README 说明后续实现不再开源，部分 PDF/OCR/旧 Office 还依赖商业组件。因此 LocalCAT 不把“追平 MateCat 80+ 格式”作为 Phase 1 目标。

必要首批：

- Project：LocalCAT JSON、TXT、PO/POT；
- 独立子规格：Ren'Py translation `.rpy`；
- Resource：现有 TMX Level 1、CSV/XLSX、归一化 TM JSON；
- 下一候选：XLIFF 1.2/2.0，但必须先定义 inline tag/state/round-trip 规则；
- DOCX/PPTX/PDF/OCR/旧 Office 等按真实需求另立规格。

参考：

- [MateCat file import](https://guides.matecat.com/file-import)
- [MateCat supported formats](https://guides.matecat.com/supported-formats-and-languages)
- [MateCat-Filters retirement](https://github.com/matecat/MateCat-Filters#retirement)
- [MateCat 当前 Filters 调用](https://github.com/matecat/MateCat/blob/develop/lib/Model/Conversion/Filters.php)

## TMX / MyMemory 上下文

MateCat/MyMemory 仍使用标准 TMX 容器并加入厂商 `<prop>`，不能表述成“完全自定义 TMX”。`OWNattempt.tmx` 仅见 `x-MateCAT-id_job`、`x-MateCAT-id_segment`、`x-MateCAT-filename`、`x-MateCAT-status`，没有 `context_before/context_after`。MateCat 在线查询/写入通过 MyMemory API 的独立参数传递前后文，因此不能假定所有导出 TMX 都携带上下文。

后续应：

- 保留未知 `<prop>` 与 provenance；
- 只有对已有样本验证的属性名做 context 映射；
- context 可选，不作为 TMX 导入成功条件；
- `TMRecord` 支持同源文、多上下文变体，避免当前 source-key 后写覆盖；
- MyMemory context profile 由独立 `tmx-context-interchange` 规格拥有。

参考：

- [MateCat 官方 TMX 样本](https://github.com/matecat/MateCat/blob/develop/tests/resources/files/tmx/exampleForTestOriginal.tmx)
- [MyMemory TMX import](https://mymemory.translated.net/doc/spec.php#TMX)
- [MateCat MyMemory context query/write](https://github.com/matecat/MateCat/blob/develop/lib/Utils/Engines/MyMemory.php)
- [TMX 1.4b specification](https://www.ttt.org/oscarStandards/tmx/tmx14b.html)

## Ren'Py 外部工具调查

`RpySeriesExtract` 与 `RpyExtended` 的 `renpy_local_tool.py` 核心状态机基本相同；Series 的 README 给出 `game → _translation_work/json|source|target → game/tl` 工程树，Extended 保留修复后产物。两者适合作为独立 plugin 的行为参照，不适合直接复制或并入 Parser Foundation：

- 都没有发现 LICENSE/COPYING/NOTICE；
- 当前实现使用 `readlines()/json.load()`，不是流式；
- placeholder 会丢失目标原文、空白、quote/newline 风格，不满足无损；
- basename 中转路径可能让同名子目录文件碰撞；
- 只可靠处理 Ren'Py 生成的 translation script，不是任意源 `.rpy`。

推荐在 `rpy-project-codec` 中独立重写 `RpyCodec + RpyDocument/RpyWriter`：有限状态机逐行读取，中立段落记录负责翻译语义，token/sidecar 负责字节级 round-trip，回填以可配 plugin 形式脱离 LocalCAT Core 本职。Parser Foundation 只定义不含 RPY 类型的 parsed-record/capability/terminal port。

`RpyExtended/tmx_extract_tool.py` 与 `/Users/pearly/Downloads/TMX/TMX提取.md` 对 locale 解析、namespace、语言对锁定和批量输出有选择性借鉴价值；`tmx_xml_template.txt` 只能作为样本，不能代替 LocalCAT 已有 TMX Level 1 安全语义。其 Ren'Py 特定的正则过滤、speaker 拆包和 source-LWW 产物不得进入通用 TMX codec，否则会把内容策略写成 Parser 语法权威。

## SQLite 与 Feature 5

SQLite canonical TM、EXACT/CONTEXT/FUZZY 检索和 Gate D 设备资格已在 TM storage/retrieval 与 Integration 线建立权威，不再是 Parser 的未来架构前置。Parser 只把 TMX 或 normalized TM JSON 解析为保序、保重复的中立 `Resource_Record`；canonical/legacy 选择、去重、迁移、staging、commit、receipt 和 retrieval capability 继续由既有 application/store owner 处理。

Roadmap Confirmed Requirements Decision 9 的 100,000 条是 TM 查询、迁移、延迟与内存资格基线，不是 Parser 记录上限。Parser 只保留现有 resource importer 的 100 MiB 输入上限和 TMX 单 segment 1,000,000 字符上限作为特定 codec 兼容事实，其他数值由版本化 limit profile 在 Design 定义并验证。

## 许可边界

- MateCat 与历史 MateCat-Filters 是 LGPL-3.0；可参考公开行为和标准，复制实现需单独合规评估。
- MateCat 当前 Filters 服务并非全部开源，不能据旧仓库承诺当前在线产品全部能力。
- 两个 RPY 辅助项目未发现许可证；在来源/授权明确前，仅做独立重写与行为参考，不复制代码或游戏数据。

## 2026-08-21 Design Discovery

### Discovery 范围

- **类型**：对现有系统的复杂扩展，采用 integration-focused light discovery。
- **正面输入**：本规格 Requirements、`rebaseline-plan.md`、当前代码/测试、Feature 5 后置 roadmap/tech/structure、`tm-storage-retrieval-index` boundary commitments、`multi-document-project-workspace` brief，以及 ADR-009/011/012/013。
- **历史治理残留**：ADR-004/005 只用于 replacement disposition，不用于证明新版依赖方向或抽象入口。
- **禁止扩展**：不实施 RPY plugin，不建立 multi-document/chunk/sync 权威，不处理 TM lifecycle 或 CONTEXT UI。

### Brownfield 集成点

| 现有入口 | 当前语法事实 | Design 含义 |
|---|---|---|
| `editor_project.py` | JSON/TXT 整体 materialize；字符串 trim；JSON 整体 fatal；TXT 非空行稠密 ID；JSON v1 临时文件 + fsync + replace | 先作为低风险 compatibility facade 迁移；保留公开签名和 Controller 错误映射 |
| `resource_importer.py` TMX | 100 MiB；拒绝 DTD/ENTITY；locale 精确/无歧义 base fallback；inline/1,000,000 超限按 record skip | 只抽出 parse/validation；canonical/legacy、digest、stage/commit/receipt 留在 application/store |
| `resource_importer.py` CSV/XLSX | UTF-8-BOM CSV；openpyxl read-only/data-only；active sheet；当前默认前两列/header/skip 规则 | 新 codec 支持显式列选择；既有入口传入前两列兼容 preset；不把 multi-sheet 解释为 Project |
| `glossary_engine.GlossaryLoader` | 重复 CSV/XLSX 规则，吞异常并直接写 Engine | 作为待委托的 compatibility consumer；最终删除重复 row parser |
| `tm_json_importer.py` | 数组根、source/target trim、坏行静默 skip、跨文件 source-LWW、直接截断写 JSONL | 分成单输入 codec + CLI/batch facade；LWW 和写入不属于 Parser |
| `tm_engine.POHandler` | 只处理极简单行，丢 `msgstr`、multiline、escape、plural/fuzzy，catch-all 后返回 partial/empty | 建立新 singular-profile gettext codec，两个 runner 迁移后从 Engine 删除该语法权威 |

### 精确迁移责任表

| 当前入口 / 责任 | 新唯一语法权威 | 保留的 Application / compatibility 责任 | 旧语法退出条件 | 相邻复验 |
|---|---|---|---|---|
| `editor_project.py` 的 LocalCAT JSON/TXT 读取与 JSON v1 写入 | `parser_localcat_codec.py` | `load_project()` / `save_project()` 保持公开签名、`EditorProject` 映射、Controller `PROJECT.LOAD_FAILED/SAVE_FAILED` 与 dirty/session 行为 | facade 已只做映射；生产代码无第二份 JSON segment 清洗、TXT 行选择或 JSON writer | `tests/test_editor_project.py`、Controller/Qt 单项目 journeys；workspace/multi-document 只重验 seam，不提前接入 |
| `resource_importer.py` 的 TMX snapshot、XML/locale/record 解析 | `parser_tmx_codec.py` | `import_tmx()` 继续拥有 canonical/legacy 选择、source digest、variant/LWW compatibility、stage/commit/receipt 和 `ImportReport` 映射 | `_parse_tmx` 等私有 XML tokenization 删除；warning/fatal 只由 codec 产生 | `tests/test_resource_importer.py`、`tests/test_tm_legacy_facade_import.py`；Integration TM owner 复核 current-source evidence/fingerprint |
| `resource_importer.py` 的 CSV/XLSX row selection | `parser_termbase_codec.py` | Controller/TermbaseStore 继续拥有 row 映射、LWW、事务、metadata 与 reload；existing import facade 保持返回形状并显式传入前两列兼容 preset | resource importer 不再独立匹配 header/列或选择 active sheet | term import/reload、列选择合同、LKG、Excel 三态与 openpyxl optional-dependency tests |
| `glossary_engine.GlossaryLoader` 的 CSV/XLSX 私有 parser | 同一 `parser_termbase_codec.py` | 迁移期 consumer adapter 只把中立 records 交给既有 Engine/Application 更新入口 | `GlossaryLoader` 不再 tokenization、吞 parser 异常或直接形成第二语法；无调用者后删除兼容 loader | Glossary self-check、LogicController/Excel 三态和术语 CRUD 回归 |
| `tm_json_importer.py` 的单文件 normalized TM JSON 读取 | `parser_tm_json_codec.py` | CLI/batch facade 继续拥有目录发现、per-file policy、跨文件 source-LWW 和兼容输出；成功终态前不得截断目标 | 单输入 JSON 记录选择只有 codec 一份；CLI 不再静默解析坏行 | 新 normalized TM JSON golden/terminal tests 与既有 CLI self-check |
| `tm_engine.POHandler` 的 gettext 解析 | `parser_gettext_codec.py` | `translation_runner.py`、`stress_runner.py` 迁移为经 composition/registry 取得 project-document codec；不在 Engine 留 re-export | 两个 runner 和 `tm_engine.py` self-check 不再引用 `POHandler`，随后删除该类 | singular/multiline/escape/header/fuzzy/plural golden；TM/runner current-source evidence 由 Integration TM owner 复核 |
| 尚不存在的 registry/composition | `parser_contracts.py`、`parser_source.py`、`parser_registry.py`、`parser_composition.py` | Application 显式声明 Effective_Purpose，并映射 Parser 结果到 Editor/TM/Termbase 合同 | 不创建 `BaseParser` facade、动态插件扫描或 Core fallback；重复 `(purpose, format)` 注册 fail closed | registry conflict、capability、AST dependency 与 plugin-missing tests |

迁移期 facade 只保护现有业务 API 与失败映射，不保留平行 tokenizer/validator。稳定的 Application API 可以长期存在；嵌在 Engine/loader 内的格式语法必须在其调用者迁移后退出。该表不授权 Parser 修改 canonical TM storage、Gate C/D、workspace 或 Qt 产品状态。

### 真实样本与参考工具

- `/Users/pearly/Downloads/TMX/MVol2Ch5_mymemory_compatible.tmx` 和 `/Users/pearly/Downloads/TMX/MVol2Ch5.tmx` 是当前兼容 TMX 回归输入，应在实施时转换为可分发的最小合成 fixture 或安全摘要，不直接提交未确认授权的全量内容。
- `/Users/pearly/Downloads/TMX/CAT_Working_File.xlsx` 有 `File_ID/Location/Speaker/Source_Text/Target_text` 且每个 sheet 对应一章；它只证明 multi-document workbook origin 的后续需求，不改变本规格 active-sheet 术语资源边界。
- `RpySeriesExtract` 的工程树与 `RpyExtended` 的 TMX/helper 只做行为参考；输入 `.rpy` 回填、token/sidecar 和安全输出全部归 `rpy-project-codec`。

## Architecture Pattern Evaluation

| 方案 | 说明 | 优点 | 风险/限制 | 结论 |
|---|---|---|---|---|
| 继续扩展 loader | 在既有入口中各自增加格式规则 | 短期 diff 小 | 多份语法权威继续分叉，无法统一 terminal/issue/capability | 拒绝 |
| `BaseParser` 名义继承 | 为不存在的 runtime 新建 ABC，并要求 codec 继承 | 名义统一 | 当前没有兼容消费者；会把归档草案变成新依赖并模糊 purpose/capability 差异 | 拒绝；replacement ADR 清理历史治理残留 |
| 用途感知 codec port | 冻结 contracts + `(purpose, format)` registry + 独立 codec + application facade | 行为可验证，不迫使 Parser 依赖 Engine/Store，能安全接 plugin | 需要分波迁移并维持兼容 facade | 选定 |

## Design Decisions

### 决策：中立合同与单输入 codec

- **选定方案**：从零建立平铺 `parser_contracts.py`、`parser_source.py`、`parser_registry.py`、`parser_composition.py` 与格式 codec；所有生产入口经既有 Application facade 渐进委托，不把归档 Parser 设计描述为现有系统。
- **理由**：符合当前平铺仓库和单向 Layer 规则，同时避免新建包层级或把 TMImporter 收入 Parser。
- **取舍**：迁移期保留 facade 和错误映射，但语法只能逐格式切到唯一 codec。

### 决策：迭代记录只在 Terminal_Success 后授权提交

- **选定方案**：迭代期记录全部 provisional；terminal 绑定 snapshot、codec/version、limit profile、记录与 warning 计数。
- **理由**：解决 fatal tail、cancel、early close 与 consumer exception 之后的 partial commit 风险。
- **取舍**：资源 consumer 必须 stage 到 terminal；commit/durability 仍由 application/store 拥有。

### 决策：外部格式只通过中立 plugin port

- **选定方案**：Parser composition 可注册符合行为/capability contract 的外部 codec；Core 不导入 plugin 类型或解释 opaque token。
- **理由**：为 RPY 回填建立 DDD 防腐层接缝，但不抢跑 RPY 规格。
- **取舍**：plugin 缺失/禁用/不兼容必须结构化 fail closed，不提供 Core fallback。

### ADR-015 的精确处置

该 candidate 只清理与当前架构事实冲突的两项历史治理，不借机扩大 Parser 范围：

| 被取代决策 | 新决策 | 兼容处置 | 可执行守卫 |
|---|---|---|---|
| ADR-004 `Parser → Engine` | Parser Foundation 与 Engine/Store 互不导入；Application facade/adapter 同时消费中立 parsed records 与既有 Editor/TM/Termbase contract，并拥有映射、事务和 receipt | 不创建 Engine data-model adapter 于 Parser 内；既有 `editor_project.py`、`resource_importer.py` 等 Application API 可保持返回形状，但逐格式退出私有 tokenizer | AST/import guard：codec/contracts 不导入 `tm_engine`、`tm_sqlite_store`、Controller/Qt；Engine/Store 不导入 `parser_*` |
| ADR-005 `BaseParser` 唯一名义接口 | codec 准入由 `(Effective_Purpose, Format_ID)`、behavior protocol 与不可变 capability snapshot 决定；重复 authority fail closed | 当前生产代码没有 `BaseParser` 或依赖它的 consumer，因此不新建 compatibility facade。若未来真实外部 consumer 出现，必须另有版本化 adapter 证据，不能反向成为 registry 准入条件 | registry contract tests：非继承实现可按行为注册；重复键、用途不兼容、plugin 缺失/禁用/版本不兼容均结构化失败 |

ADR-015 的正面依据依次为：Feature 5 后置 `tech.md` 的 Parser/Engine 相互独立边界、`tm-storage-retrieval-index` 的中立 codec 承诺、当前生产 import graph/测试，以及本规格 Requirements。ADR-004/005 的旧来源只列在 superseded history，不作为新决策理由。

### Design draft 架构骨架

```text
Application facade / adapter
  ├─ editor_project.py                 -> EditorProject / session / atomic save mapping
  ├─ resource_importer.py              -> TM/termbase stage, policy, commit, receipt
  └─ tm_json_importer.py / runners     -> batch/CLI/runner compatibility policy
                │
                v
parser_composition.py -> parser_registry.py -> selected format codec
                │                 │
                ├-----------------┴-> parser_contracts.py
                └-------------------> parser_source.py

format codecs:
  parser_localcat_codec.py   parser_gettext_codec.py
  parser_tmx_codec.py        parser_tm_json_codec.py
  parser_termbase_codec.py

tm_engine.py / tm_sqlite_store.py / glossary_engine.py
  不导入 Parser；其中历史 tokenization 在相应 consumer 迁移后退出。
```

- `parser_contracts.py`：stdlib-only 的用途、格式、snapshot、limit、capability、issue、parsed record 与 terminal dataclass/enum/protocol；不包含 Editor/TM/Qt/Plugin 私有类型。
- `parser_source.py`：regular-file/safe-root、bounded snapshot、fingerprint 与 cancellation 原语；不解释格式内容。
- `parser_registry.py`：只拥有 descriptor 注册、用途内选择与冲突拒绝；不编排 project session、resource import 或 plugin discovery。
- `parser_composition.py`：内建 codec 的显式 composition root 与中立外部 plugin registration port；不动态扫描、不提供 Core fallback。
- 格式 codec：只拥有一个输入的语法、格式 limit profile、parsed records/issues/terminal 和已声明 writer；termbase XLSX 可把 `openpyxl` 作为显式 optional dependency。

### Design draft 数据流

1. **选择**：Application 声明 Effective_Purpose 和 Format_ID/有界候选；registry 在用途内返回不可变 descriptor/capability，冲突或不支持在读取正文前失败。
2. **snapshot**：source layer 在 safe root 内打开 regular file，建立受限 snapshot identity；validation/parse/terminal 均绑定同一 identity、codec/version 与 limit profile。
3. **解析**：codec 发出 provisional records 和有界 issues；fatal/cancel/consumer exception/early close 无 Terminal_Success。
4. **提交**：project facade 只在成功终态后映射完整 Parsed_Document；resource facade 先 stage，观察 Terminal_Success 后才按既有 canonical/legacy/termbase policy 提交并生成 receipt。
5. **写入**：调用方先检查 capability；首波只有 LocalCAT JSON 走 Canonical_Write，仍由现有 facade 保持目标原子替换和稳定错误映射；其他首波格式在打开目标前拒绝 writer 请求。

### Requirements → Design draft traceability

| Requirements IDs | Design element / evidence owner |
|---|---|
| 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9 | purpose/format enums、descriptor、registry/composition、unsupported selection tests |
| 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8 | `ParsedDocument`、`ParsedSegment`、`ResourceRecord`、plugin port；application mapping boundary |
| 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10 | LocalCAT codec、project facade、canonical writer、现有 editor/controller compatibility tests |
| 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9 | gettext singular profile/state machine、PO/POT golden、runner migration |
| 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 5.11, 5.12, 5.13 | TMX/TM JSON/termbase codecs、显式术语列选择与兼容 preset、resource facade policy、multi-document/TMX-context exclusions |
| 6.1, 6.2, 6.3, 6.4, 6.5, 6.6 | validation report、ParseIssue、snapshot stale check、bounded diagnostics、source preflight |
| 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7 | provisional event stream、TerminalSuccess、per-input batch result、stage-before-commit tests |
| 8.1, 8.2, 8.3, 8.4, 8.5, 8.6 | versioned LimitProfile/cancellation、compatibility limits、100k TM boundary exclusion |
| 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7 | source preflight、strict decoding、content preservation、XML/XLSX hardening、no-network guard |
| 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7 | immutable CodecCapabilities、writer preflight、round-trip token contract、LocalCAT-only canonical writer |
| 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8 | neutral contracts、Application owner、replacement ADR、AST guard、plugin failure contract |
| 12.1, 12.2, 12.3, 12.4, 12.5 | RawSpeaker fields、format-specific validation/trim、speaker profile/device exclusions |
| 13.1, 13.2, 13.3, 13.4, 13.5, 13.6 | local identity、single-document compatibility、workspace/chunk/sync exclusions |
| 14.1, 14.2, 14.3, 14.4, 14.5, 14.6 | 精确迁移责任表、compatibility facade table、duplicate-parser exit tests、adjacent regression suites |
| 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 15.7 | golden/metamorphic/failure injection/architecture/compatibility evidence 与 deferred-feature routing |

### 设计约束归纳

- **Requirements 覆盖**：114 个 numeric acceptance criteria 均映射到 Design element 或 evidence owner。
- **终态与 snapshot**：terminal 由 Foundation guarded session 在 raw EOF 后签发；source 使用一次复制/哈希的 sealed snapshot 与 per-pass cursor lease。
- **单一语法**：canonical write 分为中立 DTO、codec serializer、Source receipt；validation/materialize 共用唯一 raw grammar与 `ValidationReport`。
- **术语列选择**：Parser read request 携带 header-name 或物理索引 selector 和显式 header policy；旧入口只通过前两列兼容 preset 保持行为，Qt 不拥有匹配算法。
- **内容与安全**：逐格式记录 trim 兼容，补齐 resource local ID、JSON 非流式能力、rooted no-follow、gettext/normalized JSON encoding、diagnostic/metadata bounds，以及 XLSX ZIP + OPC XML DTD/entity preflight。
- **限制来源**：resource 100 MiB 与 TMX 单 segment 1,000,000 字符是既有事实；其他输入、record、materialization、metadata 和 XLSX expansion 数值是 Parser v1 的版本化合同，不引用 Gate D 作为依据。

## Risks & Mitigations

- **ADR-004/005 历史语义回流**：ADR-015 已精确取代；以 ADR 索引、依赖守卫和 migration table 防止归档语义重新成为实现依据。
- **归档规格污染正面论证**：Design/ADR 只引用当前代码、Feature 5 后置 Steering、相邻正式边界与获批 Requirements；ADR-004/005 仅列为 superseded history。
- **TMX warning 与 application success 当前混淆**：ParseIssue 区分 warning/fatal，facade 显式映射到既有 `ImportReport`，不用 warning 反向授权 commit。
- **normalized TM JSON 无专属测试**：实施 Wave 0 先增 valid/warning/fatal/snapshot 合成 golden。
- **PO 现有 partial/empty 失败会被新 fatal 语义改变**：两个 runner 在 compatibility table 明示映射，不在 `tm_engine` 留 Engine→Parser re-export。
- **大文件 materialization**：每个 codec 发布版本化 limit profile；materialized view 只在上限内提供，不伪造全局 1 GB 承诺。
