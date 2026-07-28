# Feature 5 与 Qt 增量集成契约

本文件固定 `tm-storage-retrieval-index` 与 `qt-editor-json-mvp-increment` 的共享接缝。它不替代两侧 Requirements/Design；当两侧规格表述冲突时，用于识别必须回到审批阶段的跨线问题。

## 权威划分

### Feature 5 拥有

- 版本化、Qt 无关的 `SearchOptions`、命中 offsets 和 matcher capability 契约；
- Match Case、Whole Word、Unicode case-fold、词界和纯 CJK 连续匹配语义；
- legacy “区分大小写 + 允许子串”兼容 preset；
- canonical TM、exact/context/fuzzy 查询和分数/类型语义；
- query source 与实际命中 TM source 的区分。

### Qt JSON 增量拥有

- 搜索控件、字段范围、结果导航、状态反馈和“搜索全部章节”的未来入口；
- Match Case / Whole Word 的可见控件、默认选择和本地 UI 状态；
- source/target/raw speaker 的遍历、展示和高亮；
- 术语 CRUD、文字预处理和 EditorController adapter；
- capability 不足时的禁用状态及解释。

## 能力门

| Capability | Feature 5 必须证明 | Qt 允许行为 |
|------------|--------------------|-------------|
| `UNAVAILABLE` | matcher 尚未通过独立验收 | 搜索执行与高级选项均不可宣称可用 |
| `BASIC_VALIDATED` | legacy preset、稳定原串 offsets、空查询和基础 golden 回归 | 可完成基础关键词搜索；Match Case / Whole Word 仍禁用 |
| `TEXT_V1_VALIDATED` | Unicode case-fold、Whole Word、数字/下划线/标点/混合文本与纯 CJK 全部通过 | 可启用 Match Case / Whole Word，并让新术语选项参与匹配 |

Qt 不得从部分测试、类是否存在或控件是否可点击自行推断 capability。Feature 5 也不得持久化 Qt 控件状态。

## 集成顺序

1. 共享 Spec/Steering 先形成可恢复提交。
2. Feature 5 独立交付并验证 `BASIC_VALIDATED` matcher。
3. Qt 通过 `EditorController` 接入基础搜索，不直接导入 Feature 5 store/scorer。
4. Feature 5 完成 `TEXT_V1_VALIDATED` golden cohort。
5. Qt 启用 Match Case / Whole Word，并用相同 vectors 验证项目搜索、术语和 TM 调用者的一致语义。
6. Feature 5 合并到 `ui-mvp` 后完成集成验收；未验收 UI 不反向进入 Core 分支。

## 禁止的兼容捷径

- Qt、Glossary 或 TM 各自复制 case-fold、词界或 CJK 特判；
- 用 disabled 控件、空结果或 fallback matcher 伪装基础搜索已完成；
- 用 FTS/BM25 相关度替代 CAT fuzzy similarity；
- 把旧两列术语静默迁移为新默认选项；
- 让 speaker 显示名或头像参与 TM identity/context；
- 把项目章节、协作 chunk 或 Parser 格式职责塞入 matcher。

## 共同验收锚点

- legacy preset 保持现有大小写敏感子串语义；
- 纯 CJK Whole Word 与连续文本匹配结果相同；
- hit offsets 引用原始字符串且顺序稳定；
- exact TM 和 Excel 三态不回归；
- fuzzy 建议携带 query source 与 matched source，只能显式应用；
- Layer 4 只经 Controller 消费 frozen contracts。

## 重新验证触发器

- matcher semantics version、offset 单位或 capability cohort 改变；
- Qt 搜索字段、术语记录版本或默认选项改变；
- canonical TM record、speaker/context/provenance 身份改变；
- Parser/multi-document 引入新的字段范围或章节 scope；
- legacy exact、Ren'Py speaker wrapper 或 Excel 三态发生变化。
