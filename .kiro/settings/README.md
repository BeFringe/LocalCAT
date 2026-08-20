# cc-sdd 项目工作流基线

本目录保存项目级 Spec/Steering 模板，不属于任何单一 Feature。

当前文件来自用户执行的 cc-sdd OpenCode Skills 安装流程：

- `npx cc-sdd@latest --opencode-skills`
- `npx cc-sdd@latest --lang zh`

本机 npm 缓存中与这批文件对应的包版本为 `cc-sdd 3.0.2`。由于原安装命令使用了 `@latest`，后续升级不得仅依赖该浮动标签；升级提交必须记录解析到的准确版本，并先使用 `--dry-run` 审阅模板、规则、Skills 和 `AGENTS.md` 的差异。

目录归属：

- `.kiro/settings/templates/`：需求、设计、任务、research 和 steering 文档结构；
- `.kiro/settings/templates/adr.md`：项目 ADR 的统一追加式记录结构；
- `.kiro/settings/rules/governance.md`：Requirements → Design → Tasks → Feature GO 的项目治理门；
- `.opencode/skills/kiro-*`：执行审批工作流的 Agent Skills；
- `.kiro/specs/`：项目实际规格，不是模板；
- `.kiro/steering/`：项目实际长期认知与治理文件，不是模板。

模板与 Skills 应进入共享 Git 基线，使所有持久工作树使用相同流程；它们的升级必须使用独立的 `chore(sdd)` 或治理提交，不得夹带在 Feature 5、Qt 或 Parser 功能提交中。

上游 cc-sdd 保留人工 Requirements → Design → Tasks 阶段审批，并允许项目通过 `.kiro/settings/templates/` 扩展文档结构、通过 `.kiro/settings/rules/` 扩展判断标准。LocalCAT 的 ADR/Steering 门属于这一项目级扩展，不是 cc-sdd 原生 ADR 工作流；它必须维持人工批准权，且不得演化成按 task、cluster 或引用次数机械产生日志与 ADR。

2026-08-21 使用 cc-sdd 3.0.2 在 `/private/tmp` 对 LocalCAT 当前安装执行完整隔离重装：全新 `--lang zh` 会把 `specs/init.json` 的 `{{LANG_CODE}}` 渲染为 `zh`，但共享 Requirements 模板标题和示例正文仍为英文；对已经存在的 `.kiro/settings` 只重跑 `--lang zh` 时，非交互环境的默认 `prompt` policy 会保留既有文件，因此旧 `language=en` 不会被刷新。仅直接修改 `init.json.language` 也不会同步 `AGENTS.md`、`kiro-spec-init`、`kiro-spec-quick`、EARS rule 或既有 Spec，因而仍会残留英文生成提示。`--overwrite=force` 会同时刷新 36 个 Skills、16 个 Settings 文件和 `AGENTS.md`，并删除 LocalCAT 已加入的 Scope Lineage、Governance Impact、条件治理闭合、信息增量 Implementation Notes 与 Worktree Durability 规则，不可作为无差别修复。项目级 remediation 因此选择性地把默认语言固定为 `zh-CN`，同步 `AGENTS.md` 与三个 SDD language marker，并将 Requirements 的简介、边界说明、目标和验收标准模板本地化；EARS 的 `When/If/While/Where/The` 与 `shall` 固定词继续保留英文。该修订只稳定 SDD 文档生成，不改变 ADR 阶段门或任何 Feature authority，也不是 ADR 同步造成的变化。
