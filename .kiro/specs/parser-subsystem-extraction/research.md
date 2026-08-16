# Parser Subsystem 重新基线调研

## 结论

旧规格的核心目标——把文件解析从匹配引擎分离——仍然成立，但现有 `requirements.md`、`design.md` 和 `tasks.md` 不能直接实施。本目录采用**就地重新基线**：保留历史草案，先标记为非权威，再基于当前代码重写 Requirements → Design → Tasks；不删除目录，也不另建同名规格形成双权威。

## 权威顺序

1. 用户当前明确决策；
2. `.kiro/steering/` 的项目级架构与技术边界；
3. 当前分支代码和测试，作为 **as-is 行为事实**；
4. 未来经人工批准的 Parser 新版 Requirements/Design/Tasks，作为 **to-be 实施权威**；
5. README 仅作派生路线图与状态摘要；
6. 本目录遗留三文档只保留历史意图。

在新版规格获批前，代码/测试优先于旧 Parser 草案；README 也不能覆盖代码事实。新版规格获批后，它定义未来目标，实施结束再同步 README。

## 当前解析主链盘点

| 用途 | 当前入口 | 当前事实 | 重新基线处置 |
|------|----------|----------|--------------|
| 编辑项目 | `editor_project.py` | JSON 对象/数组与逐行 TXT；统一保存版本化 JSON | 迁移为 Project Document codec |
| TMX 资源 | `resource_importer.py` | Level 1、locale 选择、DTD/ENTITY 拒绝、原子合并已完成 | 作为已实现基线迁移，不重新发明 |
| 术语资源 | `resource_importer.py`, `GlossaryLoader` | CSV/XLSX 与 CSV loader 重复 | 迁移后收敛为唯一实现 |
| 归一化 TM JSON | `tm_json_importer.py` | source/target 数组导入 | 注册为 TM Resource 用途，不与项目 JSON 冲突 |
| PO/POT | `tm_engine.POHandler` | 只读取源单元，`msgstr` 未进入契约 | 迁移并明确 Project/TM 两种用途 |
| Ren'Py | 外部辅助工具 | 只可靠处理 Ren'Py 生成的翻译脚本 | 独立 `rpy-project-codec` 子规格 |

## 遗留规格的关键冲突

- `SourceUnit` 只有源文，却要求 PO `msgid/msgstr` 和 TMImporter 都使用它，类型上无法承载译文。
- “Parser 独立”同时又规定 Parser → Engine，且 TMImporter 持有具体 TMEngine。正确关系是 Application Service 协调 Parser、Store 与 Engine；Parser/Engine 只共同依赖中立契约。
- 只按扩展名注册无法区分 Project JSON 与 TM Resource JSON。注册键必须至少包含 `(purpose, format)`，冲突默认拒绝。
- `parse_file() -> list` 与 1 GB/有界内存验收矛盾。规范 API 应是 iterator；materialize 只是有显式上限的便利方法。
- `validate_file() -> bool` 无法返回失败原因，也不能用快速抽样证明完整解析必然成功；应改为结构化 `ValidationReport`。
- 没有 writer/sidecar 却承诺所有格式无损 round-trip。只有提供 DocumentCodec/Writer 的编辑格式才能承诺回写保真；TMX import-only 只承诺语义字段保真。
- “先转义再存储”和编码错误自动替换都会修改翻译数据。内容应原样进入中立契约，编码/转义只发生在具体输出边界。
- TMX 已实现，不应再列为未来 Phase 4；MyMemory context、多同源多译文仍未完成，且依赖 TMStore/Query 重构，不是仅改 XML parser。

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

`RpySeriesExtract` 与 `RpyExtended` 的 `renpy_local_tool.py` 核心状态机基本相同；Series 有 Git/README 可追踪性和更多样本，Extended 有修复后产物。两者适合作为行为参照，不适合直接复制：

- 都没有发现 LICENSE/COPYING/NOTICE；
- 当前实现使用 `readlines()/json.load()`，不是流式；
- placeholder 会丢失目标原文、空白、quote/newline 风格，不满足无损；
- basename 中转路径可能让同名子目录文件碰撞；
- 只可靠处理 Ren'Py 生成的 translation script，不是任意源 `.rpy`。

推荐独立重写 `RpyParser + RpyDocument/RpyWriter`：有限状态机逐行读取，中立段落记录负责翻译语义，token/sidecar 负责字节级 round-trip；正式仓库只提交合成 fixture。Excel 桥接保留在 Parser Core 外。

## SQLite 与 Feature 5

SQLite 不属于 Parser Foundation，也不是 Feature 5 完成后的例行优化。关键路径为：

`Parser 中立契约/现有 codec 迁移`
→ `版本化 TMRecord + TMStore/TMQuery ports`
→ `真实语料与查询 benchmark`
→ `SQLite storage/index ADR`
→ `Feature 5 context/fuzzy retrieval`

如果 Feature 5 承诺 MyMemory context、多同源变体或大库 fuzzy，SQLite/索引是其前置门；若只做小库内存 fuzzy，可由 benchmark 证明后置。Parser 始终保持存储无关。

## 许可边界

- MateCat 与历史 MateCat-Filters 是 LGPL-3.0；可参考公开行为和标准，复制实现需单独合规评估。
- MateCat 当前 Filters 服务并非全部开源，不能据旧仓库承诺当前在线产品全部能力。
- 两个 RPY 辅助项目未发现许可证；在来源/授权明确前，仅做独立重写与行为参考，不复制代码或游戏数据。
