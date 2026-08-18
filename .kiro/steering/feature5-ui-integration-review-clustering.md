# Feature 5 UI Integration 评审集群（采纳稿 v2）

本文件为 `feature5-ui-integration` 的跨 Spec 实施提供累计评审地图。它参照 Feature 5 Core 的 `feature5-review-clustering.md`，但不复制或改写 Core Gate A～D、Matcher Gate、产品 Requirements 或 task ownership。

本轮实际 dispatch 的 implementer 与 reviewer 均使用原生 subagent。评审集群的目的，是让 reviewer 在共享状态机、故障模型或 UI 旅程完整闭合后读取 `cluster-base..cluster-tip` 的累计补丁；它不减少 task-focused validation，也不把相邻 Spec checkbox 改记到 Integration。与 Feature 5 Core 的集群节奏一致，普通子任务不机械增加一次独立对抗评审；只有独立高风险缺陷已在本任务范围闭合、或累计 reviewer 给出 concrete finding 时，才按需增加定点评审。

## 与 cc-sdd 子任务完成门的关系

每个可执行子任务仍严格执行：

1. fresh implementer 读取 owning Spec，形成 Task Brief 并完成 task-focused validation；
2. parent 以 fresh evidence 验证 completion，确认当前切片不留下由后续任务补齐的本任务不变量；
3. parent 显式暂存 task 文件与 owning `tasks.md`，形成可审计的小步提交；
4. 集群全部成员提交后，dispatch 一次 cluster reviewer，读取累计 diff、共享不变量与故障矩阵；
5. cluster review 通过后运行 fresh cluster suite，并记录 base、tip 与退出证据。

cluster reviewer 只读，不取得产品或代码所有权。cluster rejection 的修正使用 implementer → task-focused validation → parent completion verification → 小步提交；随后由同一 cluster reviewer 对修正后的 tip 定点复验。若 concrete finding 本身构成独立、边界闭合的高风险切片，可额外 dispatch fresh task reviewer，但这不是固定次数门。

cluster 准备时只做无痕语义事件自检。只有实现预期改变 authority、持久格式、发布协议、依赖方向或跨 Spec frozen contract，才停止实施并形成 ADR candidate；普通实现选择和评审补救只把相对既有 Implementation Note 的信息增量记录在 owning `tasks.md`。Steering disposition 在 Feature GO 对当前结构一次收束，不按 cluster 准备/闭合次数机械写流水账。

## 推理强度

| 级别 | 适用范围 | implementer | task / cluster reviewer |
|---|---|---|---|
| `xhigh` | 崩溃恢复、durable publication、tamper、跨 generation authority | `xhigh` | `xhigh` |
| `high` | capability composition、mixed ordering、Controller state machine、Qt/macOS 集成 | `high` | 不低于 `high` |
| `medium` | 治理/身份核对、确定性文档同步、纯回归套件 | `medium` | 不低于 `medium` |

复审强度不得低于实施强度。dispatch 时若不能安全传递足够上下文，应提高而不是降低强度。

## 集群地图

| 集群 | 精确范围与 owner | 共享心智模型 | impl / cluster review |
|---|---|---|---|
| G0 — Governance, identity & exact merge | Integration 1.1～1.3 | 三方 ownership、精确 dd7 lineage、WIP 字节保护、Core merge baseline | `medium / high` |
| M — Qt maintenance | `qt-editor-mvp` maintenance ledger；快捷键、下拉框对比度、无 writable termbase 指引 | Qt native shortcut、可访问状态、非阻塞且可操作的错误反馈 | `medium / high` |
| A — Initial activation authority | Integration 2.1～2.5 | 首 generation build → seal → durable publish → reopen、rollback/recovery、LKG 与禁止 legacy fallback | `xhigh / xhigh` |
| B1 — Frozen UI state | Integration 3.1～3.3 | frozen projection、query identity、device-local preference、exact-only immutable host | `high / high` |
| B2 — Core capability composition | Integration 3.4～3.6 | Matcher Gate 与 Gate C/D 独立 authority、paired evidence、single snapshot、旧证据不可重铸 | `high / xhigh` |
| C — Resource and mixed retrieval | Integration 3.7～3.8、4.1～4.4 | immutable resource snapshot、legacy/canonical order、global top-10、局部失败、Update 权限 | `high / xhigh` |
| D — Controller TM state machine | Integration 5.1～5.6 | epoch、issued membership、stale/tamper、apply/confirm、activation completion、threshold persistence | `high / xhigh` |
| E — Qt TM surfaces | Integration 6.1～6.4 | Controller-only Layer 4、卡片/状态/阈值双入口、可访问性与真实 capability 投影 | `high / high` |
| F — Handoff & integration acceptance | Integration 7.1～7.6 | 唯一 TextMatcher handoff、真实 activated SQLite、Gate 开闭路径、mixed/stale/write/Excel 回归 | `high / xhigh` |
| Q1 — Original Qt Requirement 3 | `qt-editor-json-mvp-increment`：1.1、2.6、3.1/3.2、4.3 中只属于 Requirement 3 及其执行性前置的切片，以及对应 fresh acceptance evidence | BASIC/TEXT_V1、source/target/raw-speaker search、offset/navigation、禁止本地 matcher fallback | `high / high` |
| Q2 — Original Qt Requirement 7 | `qt-editor-json-mvp-increment`：2.7、3.4/3.5、4.5/4.7 中只属于 Requirement 7 及其执行性前置的切片，以及对应 fresh acceptance evidence | mixed termbase transaction、candidate Engine swap、configured matcher、CRUD/recovery/quarantine | `high / xhigh` |
| H — macOS LocalCAT.app | Integration 8 | bundle identity、原子安装/恢复、cwd-independent bootstrap、Finder/Dock 与 CLI/Linux 保真 | `high / high` |
| R — Cross-Spec Feature GO | Integration 9.1～9.2 | 实际结构同步、跨 Spec fresh evidence、WIP、本地性与发布裁决 | `medium / xhigh` |

Gate C/D 的名称与授权语义完全继承 Feature 5 Core。B2 只复审 Integration 如何重新计算、配对并装配 Core capability；列入本地图不把 Gate authority 转移给 Integration 或 Qt。

唯一 cluster 顺序为：`G0 → M → A → B1 → B2 → C → D → E → F → Q1 → Q2 → H → R`。这是八步 Critical Path 的评审展开，不是第二条产品路线；不得因某个 cluster 技术上可独立运行而越过前序 Checkpoint 或 owning Spec。

## Base、tip 与进入条件

- `cluster-base` 是该 cluster 首个 in-scope task commit 的父提交；`cluster-tip` 是最后一个 task 或修正提交。实际 full hash 写入 owning `tasks.md` 的 Implementation Notes 或 cluster review report，不反复改写本文件。
- G0 只在 1.1～1.3 均通过 task completion 门且精确 dd7 merge 成为可追踪 parent 后闭合。
- M 的 base 是 G0 已复审 tip；M 的 task、checkbox、commit 和 review evidence 全部归 `qt-editor-mvp` maintenance ledger。
- A 的 base 是 M 已复审 tip；未通过 M 不得开始 2.1。
- F 必须在 Q1 前闭合；Q1 base 是 F 已复审 tip，Q2 base 是 Q1 已复审 tip。
- Q1/Q2 只更新 `qt-editor-json-mvp-increment`。其既有 checkbox 混合 speaker、preprocessing、batch undo 等其他 Requirement 时，开始该 cluster 前必须在 owning Spec 完成获批的 tasks-only amendment，拆出 Requirement 3/7 及其执行性前置；不得部分勾选，也不得为省略 amendment 而把完整 mixed checkbox 的相邻产品范围拉入 Q1/Q2。
- H 的 base 是 Q2 已复审 tip；Task 8 不得凭接口存在而提前。
- R 读取 G0～H 的全部退出证据，但不以历史测试替代当前提交的 fresh Feature GO。

任一 cluster 只有在以下条件同时满足时才可复审：

1. 全部成员已经通过 task-focused checks、fresh completion verified 并小步提交；按需定点评审不得留有 unresolved finding；
2. owning worktree 中不存在未分类的新改动，用户 WIP 与不相关 untracked 未被吸收；
3. 共享状态机或 UI 旅程的正向、失败、stale/tamper 与恢复分支均已实现；
4. task-focused checks 全绿，且已知外部失败被精确记录、未掩盖新增失败；
5. parent 向 reviewer 提供 full base/tip、累计 diff、共享不变量、故障矩阵和 task reports。

## 跨 Spec 退出证据

- G0：精确 dd7 parent、Core baseline、merge 前后 WIP hashes 与三方 ownership 一致。
- M：三类 maintenance 缺陷在 macOS/Linux 语义与 offscreen journeys 中闭合，形成独立提交。
- A：首次激活的成功、取消、失败、published-tail、ambiguous facts、并发与 LKG 矩阵闭合。
- B1/B2：frozen/tamper contract、device-local threshold、Matcher Gate、Gate C/D 的开闭与 generation invalidation 闭合。
- C/D/E：mixed global top-10、resource-local failure、stale apply、Update=false 零写入与 Qt Controller-only boundary 闭合。
- F：真实 canonical SQLite + production retrieval API 证明非 100%、0.60 boundary、matched source、Gate failure 与完整回归。
- Q1/Q2：只以原 Qt Spec 的 fresh product journeys 退出，不以 Integration handoff 或 disabled 控件代替完成。
- H：真实 Finder/Dock cold launch、LocalCAT identity、cwd-independent assets/data 与 CLI/Linux regression 闭合。
- R：当前 tip 的全套业务 API、Qt/macOS smoke、治理、WIP 和本地性证据共同支持 Feature GO。

## 版本

| 版本 | 日期 | 变更 |
|---|---|---|
| v2 | 2026-08-18 | 对齐 Feature 5 Core 集群节奏：子任务以 task-focused validation、parent completion 与小步提交闭合，簇末执行一次累计对抗评审；独立高风险缺陷才按需定点评审。同步采用无痕语义事件自检、五类 ADR 门、信息增量 Implementation Note 与 Feature GO 单次 Steering 收束。 |
| v1 | 2026-08-17 | 参照 `75304b4` 建立 G0～R 跨 Spec 累计评审地图；保留逐任务 cc-sdd review 门；明确 Gate C/D authority 与 Checkpoint M/Q ownership。 |
