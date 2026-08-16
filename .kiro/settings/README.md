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
