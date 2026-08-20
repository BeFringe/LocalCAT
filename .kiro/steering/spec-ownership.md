# Spec、模板与工作树所有权

本文件区分“仓库中可见的资料”和“当前开发线可以修改的资料”。Git worktree 始终检出分支的完整仓库，因此在 Feature 5 工作树中看到 Qt 或 Parser 规格是正常现象；所有权由下表和审批阶段决定，而不是由文件是否可见决定。

## 路径职责

| 路径 | 角色 | 主要演化位置 | 提交约束 |
|---|---|---|---|
| `.kiro/specs/<feature>/` | 单项功能的 Requirements / Design / Tasks / research | 对应功能分支 | 只与该功能的代码、验证或独立 spec 阶段提交一起变更 |
| `.kiro/steering/` | 项目级长期事实、路线图、ADR 与跨线边界 | `governance/kiro-steering`，批准后合入共享基线 | 不混入单一功能实现提交 |
| `.kiro/settings/` | cc-sdd 的项目级模板和规则 | 治理分支或独立 `chore(sdd)` 更新 | 记录版本和安装来源；升级前后审阅差异 |
| `.opencode/skills/kiro-*` | OpenCode 使用的 cc-sdd Agent Skills | 治理分支或独立 `chore(sdd)` 更新 | 只跟踪技能源码；忽略依赖、缓存和锁文件 |
| `AGENTS.md` | 仓库级 Agent 工作约束 | 治理分支 | 与工作流版本保持一致，不能由功能分支临时改写 |

`.kiro/settings/` 和 `.opencode/skills/kiro-*` 必须进入共享、持久的 Git 历史，使任一工作树都能执行同一审批流程；它们不应只存在于某个 Feature 工作树，也不应被理解为 Feature 5 的组成部分。

## 当前垂直线

| 开发线 | 可写 Spec | 只读相邻 Spec |
|---|---|---|
| `feature5` | `tm-storage-retrieval-index` | 从 `ui-mvp` 基线继承的 `feature5-ui-integration`、`qt-editor-json-mvp-increment`、`qt-editor-font-zoom`、Parser 与未来格式规格 |
| `ui-mvp` | `feature5-ui-integration`、`qt-editor-json-mvp-increment`、`qt-editor-font-zoom`；维护既有 `qt-editor-mvp` 验证事实 | Parser 与未来格式规格；Feature 5 Core 正式规格由精确 merge 继承，不在 UI 线重写 |
| `parser-rebaseline` | `parser-subsystem-extraction` | `qt-editor-json-mvp-increment`、`feature5-ui-integration`、`tm-storage-retrieval-index`、`multi-document-project-workspace`、`rpy-project-codec`、`xliff-project-codec`、`tmx-context-interchange`、`speaker-display-profiles` 等相邻规格 |
| `governance/kiro-steering` | `.kiro/steering/`、ADR、项目认知治理；经审阅的 SDD 基础设施更新 | 所有功能 Spec |

`feature5-ui-integration.md` 与 `feature5-ui-integration-review-clustering.md` 是 `ui-mvp` 拥有的跨层集成 Spec；`roadmap.md`、`repository-safety.md` 与其他 `.kiro/steering/**` 共享治理文件只由 `governance/kiro-steering` 提交。任一垂直线发现冲突时可以提出修订，但治理补丁必须先在治理线形成唯一提交，再通过可追踪 merge 由活动线继承，并同时复核受影响的正式 Requirements/Design。`feature5-ui-integration` 的集成实现归 `ui-mvp`；`tm-storage-retrieval-index` 的 Core Spec 与实现归 `feature5`。

## 当前规格权威顺序

1. 已批准的 `requirements.md`、`design.md`、`tasks.md` 及 `spec.json` 审批状态；
2. 当前 Steering、跨线契约与代码事实；
3. `brief.md`、`research.md` 和旧横向规格仅作为来源与历史上下文；
4. 对话、截图和 Agent 记忆只用于抢救缺失裁决：若能从成功补丁链逐字恢复并核对原审批记录，可保留历史阶段；凡依据摘要、现状或推断重新生成/改写的内容，必须写回正式文件并重新过人工审批门。

旧 `qt-editor-mvp` 是已完成的横向基线；`qt-editor-json-mvp-increment` 是当前 Qt 单 JSON 纵向权威；`feature5-ui-integration` 是 Feature 5/Core 与 UI 的独立跨层权威；`qt-editor-font-zoom` 是独立 Qt 规格；`tm-storage-retrieval-index` 是 Feature 5 Core 唯一可写规格。`parser-subsystem-extraction` 仅由 `parser-rebaseline` 线就地重新基线；多文档与未来格式 briefs 在该线保持只读。Parser runtime 的可写代码边界须等待获批的 Design/Tasks，不由本表提前授权。

## 暂存与提交规则

1. 先按本表列出本次允许修改的路径；
2. 使用显式文件路径暂存，禁止整目录或通配暂存 `.kiro` / `.opencode`；
3. 用 `git diff --cached --name-status` 和 `git diff --cached --stat` 核对；
4. SDD 基础设施、治理、Feature 5 Spec、Qt Spec 和功能代码分别形成语义提交；
5. worktree 中存在的只读相邻 Spec 不得因“清理范围”被删除；删除会在未来合并时成为真实的仓库删除操作。
6. 不得在不同 worktree 中重新创建 patch-equivalent 提交；活动分支同步使用 merge/rebase，cherry-pick 仅限显式 backport。
