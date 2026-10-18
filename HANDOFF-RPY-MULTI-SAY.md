# Ren’Py 同原文多译句：外部评审交接

代码基线：[LocalCAT @ 3b597952](https://github.com/BeFringe/LocalCAT/tree/3b5979529ef739bc5968224b05489d3fc3cb4229)。本文供评审最小可用方案，结论再进入 R/D/T；样例均为合成文本。

## 1. 需要裁决的核心问题

用户已有 Ren’Py TL 工程，在一条原文下连续写了正文、玩家可见的译注，并穿插 `nvl clear` 和注释。游戏支持这种写法，LocalCAT 当前拒绝第二条译句，无法直接建立翻译项目。

希望在同一原文下维护有序译句组，保留播放顺序、控制行和注释。请判断：现有多译句的导入、编辑、保存和保真导出，能否作为足够小且有用的第一步；追加、拆分、合并应一起交付，还是随后补充？请比较实际使用价值和迁移成本。

## 2. 已确认的事实与复现

```renpy
define config.language = "chinese" # Language selection

translate chinese gate_001:
    # guide "We paused beside the gate."
    guide "我们在门边停下。"
    nvl clear
    # Translation note retained in the template.
    guide "（译注：这里指北门。）" # Visible to the player.
    guide "（补充：此门已废弃。）"
```

当前 `parser_rpy_codec.scan_tl` 在第 8 行返回 `PARSER.RPY.AMBIGUOUS_SLOT`。只保留第一条译句的对照可导入；语言声明、`nvl clear` 和普通注释可保留。TL 语言声明兼容已解决，连续译句仍未支持。

[Ren’Py 官方示例](https://www.renpy.org/doc/html/translation.html#more-complex-translations)明确允许一条 dialogue 原文拆成多条 say；`translate ... strings` 的 `old`／`new` 是另一种结构。玩家可见的译注属于译文，不能改成不会播放的 `#` 注释。

[TMX 1.4b](https://www.ttt.org/oscarStandards/tmx/tmx14b.html)的一个 `<seg>` 可容纳多句话，但没有直接定义 Ren’Py 多条 say 与控制行的还原规则。两者兼容，需要明确正文交换与结构还原的转换规则；TM 中的备选译法也应与连续播放的译句区分。

## 3. 当前接线与真实缺口

| 位置 | 当前事实与影响 |
| --- | --- |
| [parser_rpy_codec.py](parser_rpy_codec.py)：`_Slot`、`scan_tl`、目标替换 | 一份 source 对一份 target；放宽扫描还须解决多处替换、插入位置和控制行关联。 |
| [project_workspace_contracts.py](project_workspace_contracts.py)、[project_package.py](project_package.py) | `ProjectSegment` 与 `EditingOverlayEntry` 使用单个 `target: str`；组结构需要能保存、冷重开，并兼容旧单译句项目。 |
| [editor_controller.py](editor_controller.py)、[qt_editor_window.py](qt_editor_window.py) | RPY/workspace 走 `update_workspace_target`，既有单文档走 `update_target`；都按字符串更新，须连接编辑、校对、确认与撤销。 |
| [parser_tmx_codec.py](parser_tmx_codec.py)：`_LocaleChoice.observe` | 同一 TU 内同规范化语言取最后有效非空 TUV，不会把它们组合成有序译句；含不支持内联 XML 的 TU 会跳过。 |

项目包已有导出模板；源更新已有同标识改原文时保留译文、撤销确认，以及无法唯一关联时显示冲突的行为。新模型应复用这些能力，明确组内结构变更怎样处理。

## 4. 候选方案与编辑体验

| 方案 | 收益与主要代价 |
| --- | --- |
| **项目保存有序译句，codec 保留语法映射** | 能直接编辑并恢复 TL；需要把组结构接通项目保存和 UI。是否需要子句稳定 ID，应由具体操作决定。 |
| 先支持既有多译句的逐句编辑与往返 | 尽快解除导入阻塞；句数变化仍需外部编辑器，应评估后续追加／拆分的迁移成本。 |
| 压平译文或先转一对一 JSON | 可作临时交换；say 边界、speaker、控制行及还原责任转交外部，难以作为默认保真方案。 |

第一项是模型方向，第二项是可叠加的交付切分，请独立判断。现有模板注释先保留；额外评论系统只有在存在独立使用需求时再考虑。

低频结构操作可放在目标编辑器二级菜单：“追加译句”“在光标处拆分”“合并下一条”；“全部合并”按收益决定。校对候选为左侧一份原文、右侧按顺序纵向显示译句，长组可折叠；与轮播相比，重点看能否完整阅读、定位和用键盘操作。

## 5. 请重点判断的接缝

1. **模型与往返**：原文、译句、控制行分别如何保存；旧项目怎样兼容；不同 speaker、属性或夹有控制行时，拆分／合并怎样保留播放语义与撤销能力？
2. **确认与源更新**：同 label 改原文、模板调整 say 数量或控制行时，整组哪些内容能保留，哪些需人处理？插值与保护标签分散到多句后，哪些检查按组进行，哪些必须保留相对位置？
3. **TM／TMX**：是否以整组作为完整译法查询、应用和写回；TMX 是否输出一个正文 `seg`，是否值得带可选结构元数据；第三方不保留结构时怎样说明损失？

## 6. 最小验证与期望输出

先用上述样例贯通：导入 → 分别编辑正文和译注 → 保存 → 原模板不可用时冷重开 → 导出 TL → 重读，核对顺序、speaker、控制行、注释及未编辑部分。再覆盖旧单译句项目、strings 和一次源更新；结构操作与 TMX 仅按获选范围增加对应验证。

请给出推荐的最小范围、关键取舍、必要的 owning R/D/T 差异及少量完整任务。源码能判断的直接下结论；确有不确定性时，指出一个有退出条件的小实验。目标是让真实 TL 项目可用，不以新增通用模型或任务数量作为完成标准。
