# Brief: qt-editor-json-mvp-increment

> 权威说明：本文是 discovery 输入。当前实施以已批准的 `requirements.md`、`design.md`、`tasks.md` 为准；基础搜索也必须消费 Feature 5 的 `BASIC_VALIDATED` TextMatcher，Qt 不得保留本地 fallback matcher。

## 问题

当前 Qt 编辑器已经闭合 JSON/TXT 项目的基本翻译流程，但单个 JSON 项目中已有的独立 `speaker` 字段既没有形成按项目顺序去重、带出现次数的 raw speaker inventory，也没有在编辑和浏览中直接显示。长项目同时缺少关键词定位与受控 target 文字预处理，术语表也只能导入或从当前段追加，无法完成基础 CRUD。新的 silver logo 与资源行更多按钮尚未形成一致、紧凑的桌面呈现。

本增量只解决当前单个 JSON 项目的编辑生产力问题，不借机扩展项目格式、重做 Parser、引入模糊 TM 或提前绑定 SQLite。

## 用户裁决

1. 增量项目范围限定为当前打开的**单个 JSON 项目**；不新增 PO、RPY、XLIFF、目录批处理或多项目搜索。
2. 首批批处理的主能力是扫描当前单个 JSON 项目已有的独立 `speaker` 字段，按项目顺序去重并统计每个 raw speaker 的出现次数。
3. speaker inventory 不从 source 猜测、拆分或回填 speaker，不修改 source/target，也不改变 TM identity。
4. 第一阶段先在编辑和预览中显示 raw speaker；显示别名、显式留空和头像允许在后续阶段补充，但不得改变原始 speaker、TM key 或项目导出。
5. 第一阶段实现基础关键词搜索、简单文字预处理和术语 CRUD。
6. 基础关键词搜索默认采用不区分大小写的连续子串匹配。
7. Match Case / Whole Word 控件归 Qt MVP 增量所有，但实际匹配语义由 Feature 5 的兼容搜索引擎契约提供。该契约合并前，控件必须明确 disabled，并标注为第二阶段能力，不得伪装为已生效。
8. 桌面入口与运行窗口统一使用 `LocalCAT-logo-silver.png`；资源行 ellipsis 按内容收窄。
9. Qt 前端只调用 `EditorController` 并渲染 frozen contracts，不直接访问项目 codec、术语存储、Trie、Feature 5 搜索实现或工作区仓储。
10. 第一阶段文字预处理只修改 target。source 更新、重新导入差异判断和段落重关联不是文字预处理，后置到 Parser / multi-document project reconciliation。
11. target 变化继续撤销 `confirmed`；每次成功批量应用提供显式“撤销最近一次应用”，新的批量应用替换上一撤销点。
12. 译文框补齐 `Ctrl+Z`、`Ctrl+Y` 和 `Ctrl+Shift+Z` 的本地撤销/重做。
13. 新术语记录默认 `Match Case=false`、`Whole Word=true`；旧两列记录保持现有兼容语义，不能被静默迁移。
14. 对纯 CJK 查询，Whole Word 退化为连续文本匹配，因此勾选与未勾选得到相同结果。

## 期望结果

用户打开一个 JSON 项目后，可以先扫描已有独立 speaker 字段，得到按项目首次出现顺序排列、带出现次数的 raw speaker inventory，再在当前段和浏览预览中直接看到原始 speaker。用户还可按基础关键词在当前项目的 source/target/speaker 中定位结果，预览并显式应用经批准的 target-only 文字规则，以及在设置入口集中新增、查看、修改和删除本地术语。资源变更后，现有 Trie 立即重建并刷新当前段建议。

界面同时使用 silver logo 和紧凑 ellipsis。Feature 5 兼容搜索契约尚未接通时，Match Case / Whole Word 控件可见但不可操作，用户能清楚理解它们属于第二阶段。

## 范围

### 第一阶段范围内

- 当前单个 JSON 项目 raw speaker 的批量扫描、顺序去重与出现次数统计；
- 当前单个 JSON 项目的基础关键词搜索；
- 搜索字段限定为 `source`、`target`、`speaker`，返回结果计数、预览和前后导航；
- 原始 speaker 在当前段编辑区和浏览/校对页中的稳定显示；
- 有顺序的 target-only 普通文字规则、受影响段预览、具体差异和显式应用入口；
- 译文框本地 `Ctrl+Z`、`Ctrl+Y`/`Ctrl+Shift+Z`；
- 本地术语列表、新增、修改、删除、重复/冲突反馈和操作后热重载；
- 继续使用现有 Trie 完成运行时术语匹配；
- `LocalCAT-logo-silver.png` 的桌面入口与窗口图标一致性；
- 资源行 ellipsis 的内容自适应窄按钮；
- Match Case / Whole Word 第二阶段控件的 disabled 状态、说明文字、稳定 objectName 和可访问性名称。

### 第二阶段范围内

- 在 Feature 5 兼容搜索引擎契约合并后，将 Match Case / Whole Word 控件接到统一查询选项；
- 搜索与术语匹配可以共享兼容的文本匹配概念，但不得共享搜索会话或把项目搜索选项写入术语记录；
- speaker 显示别名、显式留空和头像可作为独立后续切片实施，并保持原始 speaker 可恢复、可访问且不进入翻译文件。

### 范围外

- 新增或迁移任何项目格式；
- 多项目、目录级或文件系统搜索；
- 修改 source、项目升级/重新导入差异判断和段落重关联；
- Replace / Replace All、正则、脚本、打开项目时自动预处理；
- 模糊 TM、context ranking、SQLite TM schema 或 JSONL TM 迁移；
- 修改 `LogicController` 的无状态三态契约；
- 将 speaker 别名/头像写入 JSON、TM key 或术语记录；
- 云端术语、远程头像、机器翻译和自动改写。

## 方案与边界

### 单项目搜索

搜索以当前 `EditorProject` 的 immutable segments 为输入，使用 Qt 无关服务返回 frozen 查询结果。结果至少包含稳定 segment id、当前索引、命中字段、字符范围和预览。结果导航由 `EditorController` 协调并复用现有段落定位，不允许 Qt 自己维护另一份项目状态。

第一阶段只提供默认基础关键词匹配。Match Case / Whole Word 的可执行语义不得在 Qt 中临时实现；第二阶段通过 Feature 5 兼容搜索契约注入统一查询选项。

### 文字预处理

本增量所称“简易文字预处理”是对当前项目 target 的受控、批量文本整理，不是应用升级或重新导入：

- 用户定义的 literal find/replace；
- 可选的空白、引号和标点规范化 preset，但每项必须显示实际规则且可关闭；
- 规则按可见顺序执行，先生成受影响段数和逐段 before/after，再显式应用；
- 不包含正则、脚本、自动翻译、source 改写、speaker/metadata 改写或打开项目时自动运行。

应用后 target 变化的段落撤销 `confirmed` 并进入 dirty 状态；预处理不得改变 source、segment id 或 raw speaker，也不得绕过现有未保存保护。每次成功批量应用提供显式“撤销最近一次应用”，撤销时恢复该批次前的 target 与 confirmed 状态；下一次成功批量应用替换上一撤销点。

译文框自身的逐键撤销/重做是独立能力：焦点在译文框时，`Ctrl+Z` 撤销最近文本编辑，`Ctrl+Y` 或 `Ctrl+Shift+Z` 重做；切换段落、应用建议和程序化刷新如何保留 undo 历史必须在 Design 中验证，不能用重新载入整段文本冒充撤销。

### 术语 CRUD

Trie 继续只负责匹配，不承担持久 CRUD。新增 Qt 无关术语记录/存储服务负责列出、添加、修改、删除、重复策略与原子写入；写入成功后由 `EditorController` 重建现有 `GlossaryEngine` 并刷新当前段建议。

第一阶段不得静默改变旧两列术语的“区分大小写 + 子串匹配”兼容行为。Feature 5 契约接入后，新建术语默认 `Match Case=false`、`Whole Word=true`；默认值必须可见、可修改并写入版本化记录，不得伪造为旧 CSV 已有字段。

### Speaker 显示

首批 inventory 只读取当前单个 JSON 各段已有的独立 `speaker` 字段，按 speaker 第一次出现的项目顺序去重并统计次数；空 speaker 段与非空 raw speaker 分开统计。不得从 source 中猜测、拆分或回填 speaker，也不得修改 source、target、confirmed 或 TM identity。

第一阶段随后直接读取现有 `EditorSegment.speaker` 并只做展示。空 speaker 保留稳定布局但不制造虚构角色。未来 display profile 只能影响呈现，必须保留 raw speaker 的 tooltip/可访问名称和安全回退。

### 依赖方向

```text
QtEditorWindow / QtSettingsDialog
              ↓
       EditorController
              ↓
Search / Preprocess / Termbase services
              ↓
Editor contracts / existing Trie / local atomic storage
```

- Qt 不导入 Engine、Repository、Codec 或 Feature 5 实现；
- 新领域服务不导入 PySide6；
- 第一阶段搜索、预处理、raw speaker 和术语 CRUD 不依赖 SQLite；
- Feature 5 只为第二阶段 Match Case / Whole Word 提供兼容搜索契约；
- Parser 或项目格式迁移后必须重新验证 JSON segment、speaker 和稳定 ID 适配，但不阻塞本 JSON-only 增量。

## 分阶段验收

### Phase 0：规格与视觉维护

- 明确记录本增量仅承诺单个 JSON 项目，不新增格式入口；
- 默认桌面 launcher 与 QApplication/窗口均使用 `LocalCAT-logo-silver.png`；
- ellipsis 按内容收窄，菜单仍可用且具备 tooltip、键盘与可访问性名称；
- Match Case / Whole Word 控件可见、disabled，并明确标注“Feature 5 合并后启用”或等价说明。

### Phase 1：Raw speaker inventory、显示与基础搜索

- 扫描当前单个 JSON 已有的独立 speaker 字段，结果按首次出现顺序去重并显示每个 raw speaker 的出现次数；
- 空 speaker 段得到独立计数，不从 source 猜测或拆分 speaker；
- 扫描前后 source、target、speaker、confirmed 和 TM identity 均保持不变；
- 当前段和浏览/校对页显示 JSON 中的原始 speaker；
- speaker 的显示不改变保存后的 raw speaker 或现有严格 speaker TM 匹配；
- 基础关键词可查询 source/target/speaker，显示结果数量与预览；
- 基础搜索默认使用不区分大小写的连续子串匹配；
- 前后导航定位到正确稳定 segment，未保存 target 不丢失；
- 空查询、无结果和项目未打开均返回结构化状态；
- Match Case / Whole Word 保持 disabled，结果不得受其视觉状态影响。

### Phase 2：预处理预览与显式应用

- 有序普通文字规则产生逐段 before/after 预览和受影响段数；
- 取消预览不改变项目；显式应用后才修改当前会话并标记 dirty；
- 只修改 target；source 更新与重新导入重关联不进入本阶段；
- 所有 target 实际变化均撤销 `confirmed`；
- segment id 与 raw speaker 始终不变；
- 每次成功批量应用提供显式“撤销最近一次应用”；再次成功应用后，上一撤销点由新应用替换；
- 撤销最近一次应用恢复该批次修改前的 target 与 confirmed 状态；
- 保存并重开同一 JSON 后可复现应用结果；
- 不提供正则、脚本、自动运行或搜索驱动 Replace All。

### Phase 2A：译文框撤销/重做

- 焦点位于译文框时，`Ctrl+Z` 撤销最近一次文本编辑；
- `Ctrl+Y` 与 `Ctrl+Shift+Z` 均可重做；
- 撤销/重做后 Controller 中的 target、dirty 和 `confirmed` 与可见文本一致；
- 全局窗口快捷键不得吞掉译文框的标准编辑快捷键。

### Phase 3：术语 CRUD 与热重载

- 术语可列出、新增、修改、删除，并在进程重启后保持；
- 重复 source、重命名冲突、无效输入和写入失败有明确结果；
- 原子失败不损坏原术语资源；
- 每次成功变更后通过 `EditorController` 重建 Trie，当前段建议与高亮立即更新；
- 旧两列术语继续采用既有匹配语义；
- disabled 的 Match Case / Whole Word 不持久化到旧 CSV。

### Phase 4：Feature 5 兼容搜索契约接入

- Feature 5 提供并通过独立测试验证的 Match Case / Whole Word 查询契约；
- Qt 只通过 `EditorController` 传递 frozen 查询选项，不直接调用 Feature 5 实现；
- 控件启用后，搜索结果与契约定义一致，禁用/启用状态均有 QtTest 证据；
- Whole Word 对纯 CJK 查询不额外施加词界过滤，结果与未勾选时的连续文本匹配相同；数字、下划线和混合文本仍需 golden cases；
- 接入不得改变术语旧记录的默认匹配行为，也不得引入 SQLite 作为 Qt 依赖。

## 回归与重新验证

- 保持现有 JSON/TXT 打开、JSON 原子保存、未保存保护、段落恢复、浏览模式、精确 TM、Trie 建议和 Excel 三态契约；
- 新 Qt 控件均有稳定 `objectName`，并扩展 AST 边界守卫，确保 Qt 仍只经 `EditorController`；
- Feature 5 搜索契约、Parser/Codec 的 JSON 迁移、术语扩展格式或 speaker profile 持久化发生变化时，重新验证本增量；
- 只有各阶段对应的可观察结果通过后，才能将该阶段标为完成；disabled 占位控件不得计作 Match Case / Whole Word 功能完成。

## 后续阶段待细化

以下内容不阻塞本轮 Requirements，但需要在各自后续阶段细化：

1. 术语修改 source 时的记录身份、排序与冲突策略；
2. speaker 别名/显式留空/头像进入后续阶段时的按项目持久化位置与迁移规则；
3. Feature 5 合并后数字、下划线和 CJK/拉丁混合文本的 Whole Word golden cases。

## 当前阶段

本文件是已进入 Requirements 阶段的 discovery 输入；正式可验收行为以同目录 `requirements.md` 为准。Requirements、Design 和 Tasks 仍需逐阶段批准，本文不是直接实施授权。
