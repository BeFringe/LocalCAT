# Brief: rpy-project-codec

## Problem

Ren'Py 译者需要在 CAT 编辑器中处理 TL 翻译模板的源文、译文和 speaker。把脚本当作普通 TXT 或逐行替换，会破坏空译文、标签、转义与非翻译内容，也无法可靠地保存确认状态。

## Current State

LocalCAT 已有中立 Parser/Codec、可配置 provider、稳定的 Project/Document/Segment 身份及 ProjectPackage 保存/重开。当前项目 intake 没有 RPY codec，通用 Application surface 尚无 source round-trip 执行入口。TM 中的 speaker 兼容桥不负责项目脚本解析。

## Desired Outcome

用户可打开受支持的单个 Ren'Py TL，在现有编辑器中翻译、确认并保存为 ProjectPackage，重开后预览并导出 TL。多个 TL 文件沿用同一项目模型，以稳定相对路径、显式顺序和源更新调和管理。

## Approach

在 LocalCAT 仓库内独立实现可配置的格式 codec/DDD 防腐层。codec 拥有语法、token、私有映射、占位符与导出；Core 消费中立段落，ProjectPackage 保存原始成员、译文/确认状态与不透明私有成员。先完成单文档闭环，再扩展到显式选择的多文档；运行时与测试均不依赖其他项目的脚本或目录。

## Scope

- **In**：TL dialogue/string 子集、speaker、空译文、old/new、结构保真、可定位诊断、ProjectPackage 持久化、导出预览/验证、显式多文件 intake 与 reconciliation。
- **Out**：任意游戏源码执行/AST、第三方宏、游戏资源打包、Excel 桥、自动目录扫描、网络同步、动态下载并执行第三方插件。

## Boundary Candidates

- RPY codec 只拥有格式映射、局部身份和源格式导出；
- Parser 提供中立能力、验证和错误接缝；
- Project 拥有项目身份、编辑状态、持久化、origin 绑定及保存报告；
- Qt 只显示 Controller 的中立编辑和导出投影；
- codec 私有成员由 Core 原样保存，不成为第二份译文权威。

## Out of Boundary

不改变 TM/术语资源权威，不把 confirmed 写入 TL，不解释 speaker 显示别名/头像，不从项目包恢复插件执行权限，不因文件后缀支持任意 Ren'Py 语法。

## Upstream / Downstream

- **Upstream**：`parser-subsystem-extraction`、`multi-document-project-workspace`、平台文件发布合同；TM/术语消费沿用 `language-resource-portability`。
- **Downstream**：本地 TL 翻译工作流；`cross-device-sync-plugin` 只搬运已经完成的项目包和资源包。

## Existing Spec Touchpoints

- **Extends**：Parser 的中立 source-round-trip 调用接缝；Project 的 codec 配置注入、private member intake 与格式导出协调。
- **Adjacent**：Qt 项目入口、speaker 显示、TM 查询兼容、ResourcePackage、协作分工均保持各自权威。

## Constraints

仅使用仓库内合成 fixture 或明确授权的测试样本。无修改导出保留原始字节；有修改只改变批准的译文跨度。不支持或过期映射不得降级猜测写回。缺失 codec 时，已有包仍可按 Project 合同编辑中立译文，但不可导出 TL。

## Promotion Clusters

1. TL 子集、中立映射、局部身份与 source-round-trip 合同。
2. 单 Document ProjectPackage 保存、重开、编辑与 TL 导出。
3. 显式多文件选择、相对路径身份、顺序与源更新调和。
4. Qt 用户旅程、故障保护与手工包消费验收。
