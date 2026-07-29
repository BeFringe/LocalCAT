# Brief: tm-storage-retrieval-index

> 权威说明：本文是 discovery 输入。当前实施以从原成功补丁链恢复并已批准的 `requirements.md`、`design.md`、`tasks.md` 为准；恢复证据见 `recovery.md`。

## Problem

个人译者已有 JSONL 精确 TM，但当前以 source 为主键和“最后写入胜出”的模型无法可靠表达同源多译文、speaker/context 变体，也不能为大库模糊建议提供稳定候选与排序。Qt 后续还需要 Match Case / Whole Word 产品能力；若各界面自行实现文本比较，兼容行为会分裂。

## Current State

`TMEngine` 在内存中读取 JSONL，并只提供精确查询。`LogicController` 保持 Excel 所需的 `TM_HIT / TERMS_FOUND / NO_MATCH` 三态，Qt 通过独立 `EditorController` 同时返回精确 TM 与术语建议。

当前没有 fuzzy/context 查询。`TMMatch` 虽已有 similarity 与 match type 字段，但它们不等于已经存在模糊检索。现有 exact key、JSONL 最后写入胜出、Active/Lookup/Update 和 Excel 三态均是必须保留的兼容基线。

## Original Feature 5 Commitment

原 Feature 5 明确提出 Levenshtein + Dice coefficient 模糊匹配。本规格保留该设计要求：

- Levenshtein 与 Dice 都是 scorer 的必选候选，需求和设计阶段不得无记录删除其中任一项；
- benchmark 可以决定两者的组合、权重、候选召回策略和阈值，但不能把 FTS/BM25 文本相关度直接冒充最终 CAT 相似度；
- 若最终只启用其中一个 scorer，必须给出真实语料证据、兼容性影响和人工批准的降级记录；
- 精确结果始终优先；模糊结果必须带分数与类型，不得自动应用。

## Desired Outcome

LocalCAT 使用可迁移的本地 TM 存储保存多个可追溯译文。查询稳定返回 exact → context → fuzzy 的有序结果；旧 JSONL 可核对迁移并兼容导出。Levenshtein 与 Dice 在同一确定性评分契约下被验证。

Feature 5 同时提供 Qt 无关的统一文本匹配兼容接口。Qt MVP 拥有 Match Case / Whole Word 的用户控件、默认选择、状态保存、导航与展示；Feature 5 只拥有选项契约、匹配语义和纯逻辑实现。

## Primary Modules

以下模块构成 Feature 5 的主闭环，必须按依赖顺序完成：

1. **Versioned TM contracts**：`TMRecord`、资源身份、provenance、speaker/context、查询与结果排序契约。
2. **SQLite TM store**：schema、事务、资源隔离、安全连接、迁移版本和索引。
3. **JSONL migration/export**：可预检、可核对、可重试；失败保留原文件；兼容导出可审计。
4. **TM retrieval**：exact 查询先达到现有行为等价，再接入候选召回、去重、阈值、上限和稳定排序。
5. **Similarity scorers**：Levenshtein 与 Dice 的纯函数实现、规范化输入和确定性 tie-break。

SQLite 已选为下一代 TM 持久化基线。ADR 决定 schema、索引、迁移与安全连接策略，不重新裁决是否采用 SQLite。

## Secondary Modules

以下模块依赖主闭环，不得反向决定存储或 exact 语义：

- **Context ranking**：在 canonical record 与 exact 等价之后增加 speaker/前后文排序。
- **Benchmark harness**：用真实、小型可提交 fixture 比较召回率、延迟、内存、Levenshtein/Dice 组合与候选索引。
- **Compatibility façades**：保留旧 `TMEngine.query_exact/save_record` 和 JSONL 导入/导出接缝，供渐进迁移。
- **Controller adaptation**：把统一查询结果适配为 `EditorController` 的 frozen suggestion；不把 Qt 类型带入 Engine。
- **TMX context interchange**：消费 canonical TM record，负责经真实样本验证的 context/provenance 映射，不拥有 fuzzy scorer。

## Unified Search Compatibility Boundary

Feature 5 提供 Qt 无关、不可变、可版本化的匹配契约，概念接口如下：

```python
@dataclass(frozen=True)
class SearchOptions:
    match_case: bool
    whole_word: bool
    semantics_version: str = "v1"

@dataclass(frozen=True)
class SearchHit:
    start_index: int
    end_index: int

class TextMatcher(Protocol):
    def find_all(
        self,
        text: str,
        query: str,
        options: SearchOptions,
    ) -> tuple[SearchHit, ...]: ...
```

语义边界：

- `match_case=False` 使用明确、可测试的 Unicode case-fold 规则；`True` 不做大小写折叠。
- `whole_word=True` 使用统一且有版本的 Unicode 边界规则；对纯 CJK 查询不额外施加词界过滤，结果与连续文本匹配相同；数字、下划线、标点和混合文本必须有 golden cases。
- `SearchHit` 使用原始字符串索引，结果按位置稳定排序且不返回零长度命中。
- 提供“区分大小写 + 允许子串”的 legacy compatibility preset，以保持现有搜索/匹配调用点可迁移。
- Qt 负责创建 `SearchOptions`、保存用户选择、展示命中和导航；Feature 5 不拥有控件、菜单、快捷键或工作区状态。
- 该接口不改变 TM exact key，也不自动改变术语记录语义；TM fuzzy、项目搜索和术语匹配只能显式选择是否复用同一文本匹配语义。

## Scope

- **In**: SQLite schema/迁移、资源隔离、多译文与 provenance、speaker/context、exact/context/fuzzy 排序、Levenshtein + Dice、阈值和结果上限、JSONL 迁移/导出、统一 SearchOptions/TextMatcher、原子失败语义、真实语料 benchmark。
- **Out**: Qt 控件与产品状态、机器翻译、语义向量、云端 TM、共享锁服务、Parser/Codec、在线协作。

## Boundary Commitments

- Parser/Codec 只产生中立格式记录与诊断，不依赖具体 TM 存储或 scorer。
- Qt 只通过 `EditorController` 和 frozen contracts 消费 Feature 5，不直接导入 SQLite store、scorer 或迁移器。
- `LogicController` 的 Excel 三态及 TM 优先行为保持不变；扩展 Excel 输出需要独立、显式版本化。
- 不从 speaker 显示名或头像反推 TM identity；原始 speaker 才能参与 context。
- 不在迁移失败后删除或覆盖旧 JSONL。

## Upstream / Downstream

- **Upstream**: 当前 exact/JSONL/Excel 行为基线；Feature 5 不等待 Parser 重构。
- **Adjacent revalidation**: purpose-aware Parser 将来替换格式适配层时，只触发 canonical record adapter 的重新验证，不反向决定 SQLite 或评分语义。
- **Downstream**: Qt Match Case / Whole Word 产品线、Qt TM 建议、`tmx-context-interchange`、未来 QA/一致性检查。

## Acceptance Anchors

- 现有 exact 输入得到同一目标；重复 JSONL 的兼容迁移结果可核对。
- exact 始终排在 context/fuzzy 前，fuzzy 不自动应用。
- Levenshtein 与 Dice 都有固定 golden vectors、边界值和确定性排序测试。
- SearchOptions legacy preset 复现现有“区分大小写 + 子串”行为；Whole Word/Unicode cases 跨调用者一致。
- SQLite/迁移/查询模块不导入 PySide6 或 xlwings。
- Excel 三态、Qt Layer 4 → Layer 3 AST 守卫和现有 Qt exact 建议旅程全部回归。
