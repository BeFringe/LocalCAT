# Parser Subsystem 就地重新基线计划

## 当前阶段

本重新基线计划已由项目 owner 批准，作为新版 Requirements 的冻结输入。Requirements、Design 与 Tasks 已获批准；`ready_for_implementation=true`，Parser runtime 仅按已批准 Tasks 分波实施。

## 保留、重写与移出

| 遗留内容 | 裁决 |
|----------|------|
| 解析与匹配分离 | 保留 |
| 注册扩展、批次错误隔离、流式等价、向后兼容 | 保留目标，重写可验收定义 |
| 单一 `SourceUnit` 承载 Project/PO/TM | 重写为用途明确的 parsed records；TM/Term 运行时记录由各自规格拥有 |
| 单文件、单文档、单项目等价 | 当前单 JSON 保持兼容；跨文档聚合移至 `multi-document-project-workspace` |
| 扩展名唯一注册、last registered wins | 重写为 `(purpose, format)`，默认拒绝冲突 |
| Parser → Engine、TMImporter 属于 Parser | 删除；移至 Application/Language Resource use case |
| `parse_file -> list` 与通用 1 GB | 重写为 iterator + 显式 materialization limit |
| 所有格式无损 round-trip | 限定到有 Writer/sidecar 的 DocumentCodec |
| TMX 作为未来格式 | 标为已实现迁移基线 |
| RPY、MyMemory context、SQLite | 分别移到独立规格；SQLite ADR 属于 TM 存储/检索规格 |
| speaker 作为正文前缀 | 重写为独立原始字段；显示名/留空/头像由 UI profile 规格拥有 |
| 33 个可选属性测试 | 收敛为少数必测跨格式不变量 + golden fixtures |

## 新版 Requirements 建议骨架

1. Parser taxonomy 与依赖边界；
2. 中立 immutable parsed records：`ParsedSegment`、格式 metadata、`ParseIssue/ParseResult`；
3. purpose-aware registry 与显式冲突策略；
4. 现有 codec 兼容与单一实现迁移；
5. fatal file / record warning / batch isolation 失败语义；
6. iterator-first streaming 与 materialization/file limits；
7. 格式特定安全策略，不修改内容；
8. Writer 存在时才要求 round-trip；
9. golden fixtures、AST 边界和现有 Qt/Excel/runner 回归；
10. speaker 原始身份的保留与不解释原则；
11. 单文档 codec 输出与多文档 workspace 的显式边界；
12. RPY、MyMemory context、XLIFF 的显式进入门槛。

## 规格拆分

- `parser-subsystem-extraction`：中立 parsed records、purpose-aware registry、JSON/TXT/PO/POT 与现有资源 codec 迁移；
- `multi-document-project-workspace`：格式中立的 `Project → Document → Segment` 聚合、origin、稳定身份、章节导航与保存失败语义；
- `rpy-project-codec`：可配置的 Ren'Py format-codec plugin，以 DDD repository 防腐层映射中立记录，并独立拥有 token/sidecar、占位符保护与可选回填/导出；
- `tmx-context-interchange`：厂商 props、上下文/provenance、TMX export 范围；
- `tm-storage-retrieval-index`：TMStore/TMQuery、多上下文主键、JSONL 迁移、SQLite ADR、exact/context/fuzzy 排序；
- `speaker-display-profiles`：按项目的 speaker 显示名/留空/头像，不改变原始身份；
- `glossary-management`：版本化术语、Match Case / Whole Word 与 CRUD；
- `editor-search-preprocessing`：快速搜索与有预览的简易文本预处理；
- `xliff-project-codec`：满足 fixture/round-trip 进入门后的 XLIFF 2.x Core 最小子集；
- 大型办公格式：按真实需求后续立项。

详细依赖顺序与跨规格边界以 `.kiro/steering/roadmap.md` 为准。

## 实施波次草案

- Wave 0：inventory、golden fixtures、现行行为基线；
- Wave 1：contracts/errors/purpose-aware registry + AST guard；
- Wave 2：JSON/TXT、现有 TMX、CSV/XLSX、normalized TM JSON 适配；
- Wave 3：PO/POT 与 CLI/runner compatibility facade；
- Wave 4：移除 `tm_engine.POHandler` 与重复 loader，同步 steering/README；
- 当前单 JSON 继续使用既有单文档路径，不依赖 `multi-document-project-workspace`；
- 分叉依赖：单个 RPY translation script 的可配置 plugin/ACL 只依赖 Parser Foundation 的格式中立端口，可独立推进；多 JSON folder、multi-sheet XLSX 与 RPY folder/project 聚合仍先依赖 `multi-document-project-workspace`；
- 并行独立规格：speaker profile、术语管理、搜索/预处理；
- 后续：MyMemory context 与满足进入门后的 XLIFF；
- Feature 5 gate：SQLite 已确定为 TM 持久化基线；storage/index ADR 决定 schema、迁移、安全连接与索引，benchmark 决定 fuzzy 候选/评分方案。

## 多文档与多章节权威边界

- Parser Foundation 负责“一个输入文档如何产生中立段落、格式 metadata、诊断和可写能力”，不负责把多个输入组合为编辑项目。
- RPY 通过可配置的 Format_Codec_Plugin 在仓库边界形成 DDD 防腐层：plugin 负责 `.rpy` tokenization、sidecar、占位符保护、安全回填、导出与 round-trip；LocalCAT Core 只消费中立记录、opaque capability 与结构化结果，不解析 RPY token，也不直接写 `.rpy`。plugin 缺失、禁用或版本不兼容时必须报告 unsupported capability，不得启用 Core fallback。
- `multi-document-project-workspace` 负责格式中立的 `Project → Document → Segment` 聚合，以及 `single_file`、`directory`、`workbook` 三类 origin。当前单 JSON 是一个项目含一个文档的既有兼容路径，不以该规格为前置。
- `document_id` 必须来自稳定 manifest ID 或规范化相对 `source_ref`；`segment_id` 使用 `(document_id, local_segment_id)` 复合身份。章节显示名、sheet 名、工作表顺序、行号和 UI 列表索引都不得单独充当持久身份。
- 章节显示与保存职责分离：workspace 拥有 `display_name/order`、切换、dirty 聚合和结构化保存报告；格式 codec/sidecar 拥有 token、原始字节映射和 round-trip。
- source 项目升级/重新导入由 workspace reconciliation 处理：基于稳定复合 ID 与 source fingerprint 区分 `unchanged/source_changed/new/removed`，`source_changed` 保留 target 但撤销确认；它不属于 Qt 文字预处理。
- 多 JSON folder、multi-sheet XLSX 和 RPY folder/project 接入均后置并依赖该 workspace 规格；单个 RPY translation script 的 plugin/ACL 不以 workspace 为前置。目录多文件不得宣称跨文件原子替换：应先 staging/validation，并逐文档报告 `saved/rolled_back/unchanged/failed`，保留未确认保存的 dirty 状态；workbook 等单文件容器仍使用完整生成、验证与原子替换。
- TMX 始终注册为 `language_resource` 互操作格式，不是 `project_document`，也不因多文档 workspace 落地而获得项目打开入口。

## 与 Feature 5 的权威边界

- 本规格定义“格式如何产生中立段落与诊断”，不定义 TM 如何存储、去重、评分或排序。
- `tm-storage-retrieval-index` 定义 canonical TM record、SQLite、exact/context/fuzzy 和 JSONL 迁移。
- TMX 是语言资源互操作用途；JSON/TXT/PO/POT/RPY/XLIFF 是项目文档用途。相同扩展名只有在 `(purpose, format)` 一致时才可竞争注册。
- 若两个规格出现交叉描述，以后批准的组件专属 Requirements/Design 为组件行为准，并必须同步回本路线图；README 只做导航，不覆盖规格。

## 下一审批点

新版 `requirements.md` 已获批准，下一审批点是项目 owner 的 Design 审阅。Design 通过本地 review gate 并生成前不改写遗留 Design；Design 与 Tasks 分别获批前不启动 Parser 代码迁移。
