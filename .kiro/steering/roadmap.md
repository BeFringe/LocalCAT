# LocalCAT 后 MVP 路线图

## Overview

LocalCAT 当前已有可运行的 PySide6 编辑闭环、单文件 JSON/TXT 项目、精确 TM、Trie 术语和 TMX/CSV/XLSX 语言资源导入。下一轮先把**单 JSON 项目**做成更完整的个人翻译工作台，同时在独立 `feature5` 分支完成 SQLite、Levenshtein/Dice fuzzy 和兼容文本搜索基建；两条线分别验收后再合并。

多文件夹 JSON、多 Sheet XLSX、RPY 和 XLIFF 项目最后推进。它们必须先经过 Parser 重新基线和 `Project → Document/Chapter → Segment` 多文档模型，不能继续扩充当前扁平 `EditorProject.segments`。

## Approach Decision

- **Chosen**: 同一干净基线上的双垂直线。
  - **Qt JSON line**：单 JSON 的 speaker 显示、基础搜索、文字预处理、术语 CRUD、图标/布局维护。
  - **Feature 5 line**：SQLite TM、JSONL 迁移、Levenshtein/Dice、统一 Match Case / Whole Word 兼容搜索内核。
- **Why**: Qt 功能不必等待全部 Parser/格式工作，但 Match Case / Whole Word 只实现一次并由 Feature 5 提供统一语义；合并前 UI 控件显式禁用，避免形成第二套匹配权威。
- **Rejected alternatives**:
  - 等所有 Core/Parser/格式完成后才做 UI：延迟单 JSON 用户价值，也不能提前验证交互闭环。
  - 先画可点击但没有真实语义的选项：会让用户误以为 Match Case / Whole Word 已生效。
  - Qt、Glossary 和 TM 各写一套大小写/词界判断：Unicode/CJK 行为会漂移。
  - 把 Parser、Docker、协作和 Qt 都塞回旧“大 Feature 5”：评审与回归范围失控。
  - 把 speaker 拼回正文或把 TMX 当作项目文档：分别污染匹配身份和项目/章节语义。

## Delivery Lanes

| Lane | 活动分支 | 当前范围 | 明确不做 |
|------|---------------|----------|----------|
| Qt JSON | `ui-mvp` | 单 JSON、raw speaker、基础搜索/预处理/术语 CRUD、silver logo、紧凑“…” | 新项目格式、SQLite、fuzzy；合并前不启用 Match Case / Whole Word |
| Feature 5 | `feature5` | canonical TM、SQLite、JSONL 迁移、Levenshtein/Dice、兼容文本 matcher、exact/context/fuzzy query | Qt 控件、Parser codec、Glossary 管理 UI、Docker/协作 |
| Parser / Multi-document | 后续规格分支 | Parser registry、Project/Document/Segment、多格式 codec | 不阻塞前两条线 |

两条活动线使用单一、可追踪的继承链：共享 SDD/Steering 与已验收 Qt 基线先在 `ui-mvp` 提交一次，`feature5` 再通过 rebase/merge 继承该历史，并只追加 Feature 5 自身规格与实现。不得在两个 worktree 中分别重建等价补丁；共享治理或跨线契约也必须只提交一次，再由活动分支继承。分支 tip 无需长期相同，但共同改动必须拥有同一提交祖先。

每个 worktree 都会看到完整仓库；Feature 5 看到 Qt、Parser 或未来规格属于正常只读上下文。实际可写范围以 `spec-ownership.md` 为准，禁止通过删除相邻规格来“清理”工作树。

## Scope

- **Now — Qt JSON increment**:
  - 从单 JSON 既有 `speaker` 字段只读盘点 raw speaker，并在编辑/浏览中独立显示；
  - 单 JSON 项目的基础关键词搜索；
  - 有顺序、可预览、显式应用的 target-only 文字替换预处理；
  - 译文框 `Ctrl+Z`、`Ctrl+Y`/`Ctrl+Shift+Z` 本地撤销重做；
  - 术语列表、新增、编辑、删除和 Trie 热重载；
  - Match Case / Whole Word 控件占位、禁用和第二阶段说明；
  - `LocalCAT-logo-silver.png` 与竖版“…”宽度维护。
- **Now — Feature 5**:
  - SQLite TM、版本化记录、多译文/context/provenance；
  - JSONL 安全迁移、兼容导出与可崩溃恢复的快照发布；
  - Levenshtein 和 Dice scorer；
  - exact → context → fuzzy 的确定性排序；
  - 供 TM、项目搜索和术语搜索消费的 Match Case / Whole Word 兼容内核。
- **Later**:
  - Parser 重新基线；
  - 多文档/多章节项目工作区；
  - 同文件夹多 JSON、多 Sheet XLSX、RPY、XLIFF codec；
  - 可跨文档任意划分/合并的协作 chunk；
  - 仿 Remotely Save 的可选跨端同步插件；
  - TMX context profile 和未来 TM Resource Editor。
- **Out of the two active lanes**:
  - 机器翻译、云端、账号、多人协作、Docker 部署和共享资源；
  - 无确认批量改写、语义向量和在线模型；
  - 任意 Ren'Py 程序源码解析；
  - 把 TMX 注册为 `purpose=project_document`。

## Constraints

- Feature 1 的 Trie 重叠匹配与长词优先是既有独立能力；Feature 5 不接管 Glossary Engine 或术语 CRUD。
- Match Case / Whole Word 的产品入口归 Qt MVP 增量，匹配契约和兼容 matcher 归 Feature 5。
- Qt 只通过 `EditorController` 和 frozen contracts 调用能力；不得导入 Feature 5 store/engine。
- 原始 speaker 用于格式与匹配身份；显示名/头像不得改写项目或 TM 键。
- exact 必须保持第一优先；fuzzy 显示分数/类型且不得自动应用。
- SQLite 已确定为 Feature 5 TM 持久化基线；ADR 决定 schema/index/migration，benchmark 决定 scorer 组合、阈值和候选策略。
- 旧 JSONL/CSV 必须可核对迁移；失败不得损坏原文件。
- Parser/Codec 保持存储无关；只有具备 Writer/sidecar 的项目格式才承诺 round-trip。
- 活动 worktree 必须位于持久文件系统；不得把 `/tmp`、tmpfs 或其他会被系统清理的目录作为唯一工作副本。
- `.kiro/` 是项目事实来源，必须由 Git 跟踪；生成或批准新的 Spec 阶段后应及时形成可恢复提交。

## Boundary Strategy

- **Feature 5 shared seam**: `SearchOptions(match_case, whole_word)`、文本规范化/词界判断和稳定 hit offsets；UI/Glossary/TM 只消费，不复制实现。
- **Durable snapshot seam**: export/refresh/recovery 以 durable receipt/handoff 为状态权威，文件变更必须绑定完整 parent chain 与 exact inode identity，在最后一次 mutation 前复证 source/destination，并以 post-mutation fsync/身份复核和冷恢复共同闭合；任何调用方不得以相同字节代替该命名空间证明。
- **CJK Whole Word**: 对纯 CJK 查询不施加额外词界过滤，结果与未勾选 Whole Word 的连续文本匹配相同；该退化必须是明示、版本化且有 golden cases 的兼容语义。
- **Qt Stage A**: 搜索 UI、结果模型和导航可以先实施，但基础搜索只有在 Feature 5 legacy matcher 达到 `BASIC_VALIDATED` 后才能完成验收；两个高级选项保持 disabled，且不得写入持久记录。
- **Qt Stage B**: 合并 Feature 5 后启用 Match Case / Whole Word，并用跨 source/target/speaker/术语 fixture 验证一致结果。
- **Parser seam**: Parser 产生带 source reference/speaker/metadata 的 Document/Segment；不定义搜索、TM 排序或 speaker 显示。
- **TMX seam**: TMX 始终属于语言资源 import/export。若未来需要编辑 TMX，另立 TM Resource Editor。

## Existing Spec Updates

- [ ] `qt-editor-mvp` -- 收口已完成 MVP 状态；只保留 silver logo 与“…”按钮维护事实。Dependencies: none
- [ ] `parser-subsystem-extraction` -- 后置重新基线，加入多文档/多章节契约与 TMX 非项目格式边界。Dependencies: Qt JSON + Feature 5 合并不要求等待它

## Specs (dependency order)

- [ ] `qt-editor-json-mvp-increment` -- 单 JSON 的 speaker、基础搜索/预处理/术语 CRUD 与第二阶段选项入口。Dependencies: `qt-editor-mvp`
- [ ] `tm-storage-retrieval-index` -- SQLite、JSONL 迁移、Levenshtein/Dice、兼容文本 matcher 和 exact/context/fuzzy。Dependencies: current exact/JSONL behavior baseline
- [ ] `glossary-management` -- 启用每术语 Match Case / Whole Word、版本化记录与导入迁移。Dependencies: `qt-editor-json-mvp-increment`, `tm-storage-retrieval-index`
- [ ] `editor-search-preprocessing` -- 启用项目搜索 Match Case / Whole Word、扩展结果语义与大项目优化。Dependencies: `qt-editor-json-mvp-increment`, `tm-storage-retrieval-index`
- [ ] `speaker-display-profiles` -- 每项目 speaker 显示名/留空/头像。Dependencies: `qt-editor-json-mvp-increment`
- [ ] `multi-document-project-workspace` -- Project/Document/Segment、章节导航、稳定复合 ID 和多文档保存报告。Dependencies: `parser-subsystem-extraction`, `qt-editor-json-mvp-increment`
- [ ] `collaborative-job-chunks` -- 在不改变 Document 身份的前提下按稳定 segment 集合划分、合并和分配协作 chunk。Dependencies: `multi-document-project-workspace`
- [ ] `cross-device-sync-plugin` -- 本地优先的可选同步插件边界、远程 provider、冲突保护与凭据安全。Dependencies: `multi-document-project-workspace`
- [ ] `rpy-project-codec` -- Ren'Py translation script 独立解析与安全回写。Dependencies: `parser-subsystem-extraction`, `multi-document-project-workspace`
- [ ] `tmx-context-interchange` -- 经验证的 TMX props/context/provenance。Dependencies: `tm-storage-retrieval-index`
- [ ] `xliff-project-codec` -- XLIFF 2.x Core 最小项目 codec。Dependencies: `parser-subsystem-extraction`, `multi-document-project-workspace`

## Deferred Format Backlog

- **同文件夹多 JSON**：文件夹/manifest 为 Project，相对 JSON 路径为 Document；需要稳定排序与部分保存报告。
- **多 Sheet XLSX**：workbook 为 Project，受支持 Sheet 为 Document；`File_ID` 是身份，Sheet 名只作显示。首个真实样本 `CAT_Working_File.xlsx` 有 34 个 Sheet、17,255 条数据行。
- **RPY folder**：每个 translation script 为 Document；需要 token/sidecar。
- **XLIFF**：通过真实 fixture、inline-code 和 Writer capability gate 后实施。
- **多文档 UI**：Document 按导入/manifest 顺序连续导航，在编辑和浏览/校对模式提供章节下拉与分隔；迁移搜索 UI 时使用“搜索全部章节”，内部采用可扩展 `SearchScope`，为未来 `current_chunk` 留口但不把 chunk 当成章节身份。
- **协作 chunk**：可按稳定 segment 集合或连续范围跨文档任意划分、合并和分配；它是协作视图，不改变 Project/Document/Segment 的规范身份。
- **跨端同步**：独立可选插件，只同步经批准的项目包/资源及 chunk metadata；不把远程副本变成项目权威，也不把文件同步冒充实时协作。

## Merge Contract

1. **Feature 5 gate**：Levenshtein/Dice、exact parity、SQLite migration、snapshot namespace/crash-recovery 故障矩阵、激活后 CURRENT/HISTORY/DIVERGED 冷重开的 canonical authority 且不回退 JSONL、Match Case / Whole Word Unicode/CJK fixtures、无 Qt import 全部通过。
2. **Qt gate**：单 JSON 的基础搜索/预处理/术语 CRUD、raw speaker、silver logo 和 disabled 选项通过；既有 JSON/TXT、TMX/CSV/XLSX 资源、Excel 三态回归保持。
3. **Merge direction**：`feature5` 从最新已验收 `ui-mvp` 基线分叉；活动期间的共享更新通过 merge/rebase 继承，不重建等价提交。Feature 5 通过独立 gate 后合并回 `ui-mvp`，再由 UI 线新增 Controller adapter 并启用两个控件。
4. **Integration anchor**：相同 `SearchOptions` 对项目 source/target/speaker、术语和 TM 候选产生一致的 case/word-boundary 语义；exact TM 与 legacy Excel 输出不变。

## Confirmed Requirements Decisions

1. Qt JSON 首批预处理只修改 target；source 更新、重新导入差异和段落重关联属于后续 Parser / multi-document project reconciliation。
2. target 内容变化撤销 `confirmed`，沿用当前编辑会话行为；译文框必须提供 `Ctrl+Z`、`Ctrl+Y`/`Ctrl+Shift+Z`。
3. raw speaker 先行；alias、显式留空和头像后置。
4. 新术语记录默认 `Match Case=false`、`Whole Word=true`；旧两列记录不静默改变。
5. 纯 CJK 查询在 Whole Word 下退化为连续文本匹配，因此与未勾选 Whole Word 的结果相同。
6. `.kiro` 必须保持 Git 可跟踪；此前“完成 Qt JSON 与 Feature 5 后再解除忽略”的决定因可能丢失唯一 Spec 副本而撤销。
7. raw speaker 批处理以单 JSON 现有 `speaker` 字段的扫描、去重、计数和顺序盘点为主；不从 source 猜测或拆分 speaker，也不改写 source/target。
8. target 批量预处理使用独立的“撤销最近一次应用”，译文框本地 `Ctrl+Z`/`Ctrl+Y` 仍保持独立。
9. Feature 5 的 100k TM 性能门以 warm exact p95 ≤ 50 ms、fuzzy top-10 p95 ≤ 500 ms、迁移 ≤ 120 s、内存 ≤ 512 MiB 为基线，并记录测试环境。
10. fuzzy 建议同时携带查询 source 与实际命中的 TM source，既保护过期应用校验，也解释相似匹配。
11. 多文档搜索迁移时，用户入口使用“搜索全部章节”；内部 scope 为后续 `current_chunk` 留扩展口，但 chunk 的任意拆分/合并另立协作规格。
