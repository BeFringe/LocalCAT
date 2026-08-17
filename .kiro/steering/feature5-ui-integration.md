# Feature 5 与 Qt 增量集成契约

本文件固定三个 Spec 的责任边界：`tm-storage-retrieval-index` 拥有 Feature 5 Core，独立 `feature5-ui-integration` 拥有跨层装配与 UI frozen contract，`qt-editor-json-mvp-increment` 继续拥有单 JSON 产品功能。它不替代三侧 Requirements/Design；表述冲突时必须回到相应审批阶段，不能用既成实现扩张范围。

## 权威划分

### Feature 5 Core 拥有

- 版本化、Qt 无关的 `SearchOptions`、命中 offsets、`TextMatcherState` / `TextMatcherCapability`；
- Match Case、Whole Word、Unicode case-fold、词界和纯 CJK 连续匹配语义；
- canonical SQLite TM、exact/context/fuzzy 查询、候选证明和分数/类型/排序语义；
- `TMQuery`、`TMResourceHandle`、`QueryReport`、resource-local failure 与 retrieval capability；
- query source 与实际命中 TM source 的区分。
- `tm-storage-retrieval-index` Requirement 2 已批准的首次迁移/激活语义；精确 dd7 merge 后由 Core boundary 补齐 application-facing `activate_initial()` 公开合同。

### Feature 5 UI Integration 拥有

- 从精确 `feature5@dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b` 形成可追踪 merge；
- Qt 无关 application composition root、declarative resource resolver、正式 matcher/retrieval publisher 装配；
- canonical/legacy resource port 与 Controller adapter，及 `QueryReport → TMSuggestion` / resource-status 映射；
- Controller current-segment TM use cases 与 TM suggestion UI；即使实现文件位于 Qt Layer 4，也不转记给原 Qt Spec；
- 当前段 canonical exact/context/fuzzy 建议、mixed legacy/canonical 全局 top-10、device-local 60% 阈值；
- 显式 apply 与 stale 拒绝、capability/failure 的诚实降级投影；
- Feature 5 `TextMatcher` 向原 Qt Requirement 3/7 的正式 handoff；
- macOS `LocalCAT.app` 的轻量入口及 bundle identity 验收。

### Qt JSON 增量拥有

- Requirement 3 的搜索控件、字段范围、结果导航、状态反馈和单 JSON 基础关键词搜索产品完成；
- Match Case / Whole Word 的可见控件、默认选择和本地 UI 状态；
- source/target/raw speaker 的遍历、展示和高亮；
- Requirement 7 的术语 CRUD、管理页、configured matcher 对接与 Trie 热重载；
- speaker inventory、target-only 预处理、批量撤销，以及既有编辑旅程和快捷键；
- integration handoff 后相应产品控件的最终验收。

`feature5-ui-integration` 只交付正式接缝和本次新增的 TM 建议产品合同，不把 Requirement 3 搜索产品或 Requirement 7 术语 CRUD 改记到自己名下。

## 能力与状态投影

| Core 权威 | Integration 输出 | Qt 允许行为 |
|---|---|---|
| `TextMatcherState.UNAVAILABLE` | 安全、non-authoritative 的不可用投影 | 不宣称搜索或高级 matcher 选项可用 |
| `TextMatcherState.BASIC_VALIDATED` | BASIC 支持投影 | 可执行基础关键词搜索；Match Case / Whole Word 禁用 |
| `TextMatcherState.TEXT_V1_VALIDATED` | TEXT_V1 支持投影 | 可启用 Match Case / Whole Word，并让 configured terms 使用同一 matcher |
| retrieval capability closed/stale | exact-only、degraded 或 unavailable 安全投影 | 不显示 fuzzy 可用，不把失败伪装成零结果 |
| resource-local failure | 独立 resource status | 保留其他资源结果，不把 failure 伪造成 suggestion |

`degraded` 只是 UI 展示投影，不是新的授权状态。UI 不得定义第二份 `MatcherCapability` / `MatcherReadiness`，也不得从布尔值、局部 `PASS`、store health 或 FTS 可用性反向开放 fuzzy。

## 唯一 Critical Path

owner 指 Spec、task checkbox、代码边界与验收权威，不指 Agent 或 thread。同一 Codex thread 可以依次执行不同簇，但进入每一簇时必须重新载入 owning Spec，并只更新该 Spec 的 Tasks。

| 顺序 | 簇 | 归属 |
|---|---|---|
| 1 | Integration Requirements → Design → Tasks 全部批准 | `feature5-ui-integration` |
| 2 | 精确 merge `feature5@dd7c9f…`，运行 Core Gate A/B/C/D、Matcher Gate 既有实现基线 | Integration merge |
| 3 | Checkpoint M：组合键、下拉框对比度、无 writable termbase 指引 | `qt-editor-mvp` maintenance ledger |
| 4 | 补 `activate_initial()` 并通过首次激活发布/恢复合同 | Feature 5 Core boundary；任务记在 Integration |
| 5 | contracts → Matcher/Gate C/Gate D composition → resolver → adapter → Controller → TM suggestions → Task 7 全验收 | `feature5-ui-integration` |
| 6 | Checkpoint Q1：单 JSON Req3 search | `qt-editor-json-mvp-increment` |
| 7 | Checkpoint Q2：Req7 术语 CRUD/管理入口 | `qt-editor-json-mvp-increment` |
| 8 | Controller/TM UI 与 Checkpoint Q 验收后独立提交 macOS `LocalCAT.app` | `feature5-ui-integration` |

Gate A～D 与 Matcher Gate 只使用 Feature 5 Core 定义；跨 Spec 等待点只称 Checkpoint M/Q。Gate C 证明 context/fuzzy-core correctness，Gate D 按 FTS5/fallback intended path 证明 fuzzy benchmark；二者及 Matcher Gate 均不迁移资源，也不从 physical canonical state 推断能力。详细进入/退出条件由 Integration Design 自包含定义。

## 禁止的兼容捷径

- Qt、Glossary 或 TM 各自复制 scorer、case-fold、Whole Word、CJK、capability evaluator 或候选证明；
- Qt 直接导入 SQLite store、retrieval、evidence evaluator 或 candidate proof；
- 用 disabled 控件、空结果、调用方布尔值或 fallback matcher 伪装能力已完成；
- 把 legacy JSONL 资源因 mixed 模式提升为 fuzzy，或在启动/打开/查询时静默迁移；
- 让 speaker 显示名或头像参与 TM identity/context；
- 把项目章节、协作 chunk 或 Parser 格式职责塞入 integration。

## 共同验收锚点

- legacy importer 的 source-LWW 折叠与当前 100% suggestion 卡片只证明 legacy exact compatibility；
- canonical 多译文、非 100% 相似度、0.60 阈值、fuzzy 排序与 mixed global top-10 必须用真实 SQLite generation 和 production `TMRetrievalService` 验收；
- legacy exact-only 与 canonical exact/context/fuzzy 可混合查询，资源局部失败不吞掉其他成功结果；
- exact 优先、raw speaker identity、Excel 三态、Trie 术语、JSON/TXT 与本地性不回归；
- fuzzy 建议携带 query source 与 matched source，只能显式应用且 stale apply fail-closed；
- Layer 4 只经 `EditorController` 消费 frozen contracts。

## 重新验证触发器

- matcher semantics version、offset 单位或 capability cohort 改变；
- retrieval capability、proof query version、排序、阈值范围或 global limit 改变；
- canonical TM record、speaker/context/provenance 身份或 resource activation 生命周期改变；
- Qt 搜索字段、术语记录版本、默认选项或 `TMSuggestion` 形状改变；
- Parser/multi-document 引入新的字段范围或章节 scope；
- legacy exact、Ren'Py speaker wrapper、Excel 三态或 macOS bootstrap 改变。

## Governance Sync Record

- 2026-08-17：ADR-007～011 已采纳，ADR-002/006 的 canonical 范围部分被 ADR-007 取代。
- 2026-08-17：新增 `feature5-ui-integration` 独立 Spec；本文件同步为三方 ownership 与验收边界。
- 2026-08-17：跨 Spec 顺序冻结为八步 Critical Path；外部等待点改称 Checkpoint M/Q，避免与 Feature 5 Gate A～D 冲突。
