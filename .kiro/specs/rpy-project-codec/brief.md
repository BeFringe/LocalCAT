# Brief: rpy-project-codec

## Problem

Ren'Py translation script 同时包含 speaker、对白、标签、缩进、注释和占位符。把它当普通 TXT 或简单正则替换会丢失结构，也无法安全回写。

## Current State

LocalCAT 只有针对 `speaker "text"` TM 记录的严格兼容桥，没有 `.rpy` 项目 codec。`RpySeriesExtract` 与 `RpyExtended` 可提供状态机行为和样本线索，但未发现许可证，且现有实现不满足流式与无损目标。

## Desired Outcome

用户可打开受支持的 Ren'Py translation script，在独立 speaker 列中编辑对白，并在不破坏非翻译内容、占位符和文件风格的前提下保存。

## Approach

独立实现有限状态机 codec；翻译语义映射到中立段落，原始 token/sidecar 保存回写所需结构。只承诺 Ren'Py 生成的 translation script 子集，不解析任意游戏程序逻辑。

## Scope

- **In**: 受支持 dialogue/string blocks、speaker、old/new 文本、占位符、注释/空白保护、诊断、golden fixtures、原子回写。
- **Out**: 任意 `.rpy` 程序执行语义、AST 解释器、游戏资源打包、Excel 桥、从无许可证项目复制代码。

## Boundary Candidates

- RPY tokenization/state machine；
- 段落映射与 speaker 提取；
- document sidecar；
- writer 与 round-trip 验证；
- 不支持语法的诊断/跳过策略。

## Out of Boundary

- 不拥有 speaker 显示别名/头像；
- 不拥有 TM 存储或 fuzzy；
- 不承诺所有 Ren'Py 版本与第三方宏。

## Upstream / Downstream

- **Upstream**: `parser-subsystem-extraction` codec 契约和错误分类。
- **Downstream**: Qt 编辑/浏览、speaker profile、TM query context。

## Existing Spec Touchpoints

- **Extends**: Parser rebaseline 中已拆出的独立 codec。
- **Adjacent**: `renpy_tm_compat.py` 只处理 TM 查询别名，不是 RPY parser。

## Constraints

使用合成 fixture 或用户明确授权的样本；失败不得修改源文件；只有已验证的语法子集进入支持清单。
