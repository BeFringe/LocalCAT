# Brief: xliff-project-codec

## Problem

XLIFF 是跨 CAT/TMS 的交换格式，但完整标准包含内联代码、模块、状态和扩展。没有真实互操作边界就直接“支持 XLIFF”会造成可打开却无法安全回写的伪兼容。

## Current State

LocalCAT 不支持 XLIFF。现有 Parser 重新基线已经把大型/复杂格式放到独立规格，不应阻塞 JSON/TXT/PO/RPY 主线。

## Desired Outcome

在满足进入门后，用户可打开并安全保存明确支持的 XLIFF 2.x Core 子集；segment/source/target/state 和可验证的 speaker metadata 能映射到编辑器，未支持结构得到明确诊断。

## Approach

以 OASIS XLIFF 2.1 Core 为规范基线，并覆盖其要求的 2.0 兼容行为；先用少量真实 fixture 定义 capability matrix，再决定 Writer/sidecar 范围。XLIFF 1.2 作为独立兼容面，不在首批默认承诺。

## Scope

- **In**: XLIFF 2.0/2.1 Core 最小子集、file/unit/segment、source/target、language/state、受控内联代码保护、可验证 speaker metadata、诊断与原子回写。
- **Out**: XLIFF 1.2、全部 2.x modules、厂商全部扩展、Skeleton/原始二进制重建、作为 TM 存储。

## Boundary Candidates

- XLIFF core reader；
- inline code/token sidecar；
- state/language/speaker metadata 映射；
- writer capability matrix；
- unsupported-module diagnostics。

## Out of Boundary

- 不把未知 metadata 自动解释成 speaker；
- 不承诺丢弃未知模块后仍可 round-trip；
- 不阻塞优先级更高的 RPY 和 TM 存储工作。

## Upstream / Downstream

- **Upstream**: `parser-subsystem-extraction` codec 契约。
- **Downstream**: Qt 编辑/浏览、speaker profiles、跨工具项目交换。

## Existing Spec Touchpoints

- **Extends**: Parser rebaseline 的后续格式进入门。
- **Adjacent**: TMX 仍是语言资源交换；XLIFF 是项目/本地化工作流交换。

## Constraints

正式设计以 [OASIS XLIFF 2.1](https://docs.oasis-open.org/xliff/xliff-core/v2.1/xliff-core-v2.1.html) 为主规范；进入门要求真实 fixture、明确 speaker 映射和可验证的 round-trip 保护范围。
