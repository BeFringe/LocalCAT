# 研究与设计决策

## Summary

- **Feature**：`rpy-project-codec`。
- **Discovery Scope**：既有 Parser/Project 的集成扩展。
- **Key Findings**：ProjectPackage 已分离 source bytes、中立 document/overlay 和 opaque private member；单文件 intake 与通用 round-trip 执行是需要补齐的接缝；保真必须建立在精确定义的 TL 子集上。

## Research Log

### TL 子集与字符串语义

- **Sources Consulted**：[Ren'Py Translation](https://www.renpy.org/doc/html/translation.html)、[Language Basics](https://www.renpy.org/doc/html/language_basics.html)、[Text](https://www.renpy.org/doc/html/text.html)、[CLI](https://www.renpy.org/doc/html/cli.html)。研究对象为官方 8.5.4 文档。
- **Findings**：TL 有 dialogue 与 old/new strings 两类翻译入口；Ren'Py 还允许复杂重排、Python 和多行文本，不能因为后缀相同就认为可安全回填。语言标识不是 BCP 47；文本含不应执行的 interpolation 与 tags。
- **Implications**：首个 profile 限定可一一证明的翻译槽，未知执行语法拒绝；引擎不是 LocalCAT 的运行时依赖。空译文保留，空 source 因中立合同不支持而明确拒绝。

### Project 持久化与配置入口

- **Sources Consulted**：[parser_contracts.py](../../../parser_contracts.py)、[parser_composition.py](../../../parser_composition.py)、[project_workspace_contracts.py](../../../project_workspace_contracts.py)、[project_workspace_intake.py](../../../project_workspace_intake.py)、[project_package.py](../../../project_package.py)。
- **Findings**：非 legacy SINGLE_FILE workspace 可以用 ProjectPackage；实际 OriginBinding/intake 仍限定多文件 profile。包内 documents JSON 已保存 source facts 与 editing overlay；raw source 和 private bytes 各自有成员。打开包不需要 codec。descriptor 尚无 round-trip factory；禁用 ProviderBinding 会抛错，不能直接混入有效 surface。
- **Implications**：新增通用单文件 binding/profile 和注入 seam，维持现有 carrier；composition 只实例化启用的已知 provider。首次包保存必须保持 rooted source binding，不能只有内存段落。

## Architecture Pattern Evaluation

| 方案 | 优点 | 局限 | 结论 |
| --- | --- | --- | --- |
| 独立 JSON 与外部 sidecar | 文件直观 | 配对丢失、第二状态权威和双文件恢复 | 不采用 |
| 单 Document ProjectPackage | 原始内容、私有映射、编辑状态自包含 | 需要通用单文件 intake 增量 | 采用，用户已选择 |
| 执行 Ren'Py AST/引擎提取 | 可覆盖复杂脚本 | 引入执行语义及大依赖，不满足有限 codec 边界 | 不采用 |

## Design Decisions

### Decision：仅泛化调用接缝

Parser 增加中立 prepare 接口，不加入 RPY 字段；codec 自己持有格式规则。Project 继续只搬运 opaque bytes。grammar 独立实现于本仓库，fixture 同仓库维护，无其他项目运行依赖。

### Decision：准备与发布分离

复用现有 rooted 发布原语和 token 校验思想，补上 source/target/preview 的绑定；不把 canonical serializer 冒充 round-trip writer。按 ADR-026 保持 Parser writer 无状态，Project/Resource 的 journal 不移入 codec。

## Risks & Mitigations

- 表面相似但非受支持 TL：用 grammar 拒绝矩阵与位置诊断，禁止宽松正则回退。
- 源变更改变 Ren'Py label：保留 Project 的 new/removed/ambiguous 与显式映射，不猜测近似文本身份。
- 恶意 private offsets：live codec 从原始 source 重建并核对映射；Core 不信任 payload 内部声明。
- 相邻合同尚未批准：Design 明确 pending/NO-GO，任务图是可审阅提案，不先改 runtime。

## References

- [ADR-015](../../steering/adr/adr-015.md)：Parser/codec 分权。
- [ADR-018](../../steering/adr/adr-018.md)、[ADR-019](../../steering/adr/adr-019.md)：中立项目及严格包载体。
- [ADR-026](../../steering/adr/adr-026.md)：Parser 无状态写入边界。
