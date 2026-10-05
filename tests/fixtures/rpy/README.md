# RPY 合成夹具

这些输入全部为仓库内新写的合成内容，不含用户游戏正文，不读取外部项目、音频或 Ren'Py 安装。`voice` 路径和 interpolation 名称只是惰性测试字符串。

本目录为 `rpy-project-codec` Task 1.1 的规格期望材料。`manifest.json` 的 `expectation_kind=specification-oracle` 表示期望来自已批准的有限 TL profile；夹具契约测试通过不表示 RPY codec、回填、项目包或运行时已经实现或通过验收。

运行内容契约验证：

```sh
python3 -m unittest tests.test_rpy_fixture_contract -v
```

`manifest.json` 使用以下约定，后续 codec golden 可以直接消费：

- `payload.kind=file` 直接读取原字节；`kind=hex` 将 ASCII 十六进制解码为原字节。损坏 UTF-8 使用 hex 文件，以免编辑器替换非法字节。所有路径相对于本目录，必须位于 `payloads/`。
- `byte_features` 绑定输入字节长度、SHA-256、UTF-8 有效性、BOM、换行形式与末尾换行。`.gitattributes` 禁止 Git 改写 `.rpy` 换行；BOM/CRLF 案例保存的就是实际 BOM/CRLF 字节。
- `expectation.classification` 为 `supported` 或 `rejected`，描述整个输入的规格分类。拒绝案例的 `records` 和 `slots` 必须为空，包括先合法、后非法的完整文档。
- 支持案例的 `records` 是按显示顺序排列的中立 `source`、`target`、`speaker`、`confirmed`；字符串均已解码，narrator/strings 的 speaker 为 `null`，空 target 保留，首次导入的 confirmed 为 `false`。
- `slots` 的 `record_index` 从 0 开始；`identity_basis` 保存 dialogue 的原始 language/label，或 strings 的 language/完整解码 old 的 UTF-8 SHA-256。它是稳定身份的期望依据，不指定尚未实现的局部 ID 序列化形式。
- `source_anchor`、`target_anchor` 给出原始行号、prefix、含引号的 literal、suffix，以及含引号的半开字节区间 `literal_byte_span=[start,end)`。偏移从原始文件第 0 字节算起，包括 BOM；这只是可核对的夹具定位数据，不冒充私有 payload 或 writer 权限。
- `preserved_lines` 明确列出不产生记录的 header、comment、blank、control。它们与全部 source/target anchor 必须恰好覆盖每一物理行。speaker 属性、transition 留在 prefix/suffix 中，不进入中立文本。
- 拒绝案例的 `diagnostics` 给出分类、中文原因和真实 witness。`line` 与 `byte_column` 从 1 开始，后者为字节列；`byte_offset` 从 0 开始。重复身份定位第二次出现的位置。分类与 witness 描述失败边界，不冻结未来 Parser 诊断代码或要求生产诊断暴露正文。

支持矩阵包括同文件交替 dialogue/多 strings block、narrator、同源文不同 label、普通/负属性与临时属性、extend/centered、transition、voice/nvl clear、纯控制块、空 target、None-language strings、单/双引号、声明转义、Unicode、引号/井号、interpolation/text tags、BOM/CRLF 与无末尾换行。

拒绝矩阵包括损坏编码、未声明转义、物理多行/三引号/未闭合字符串、pass、缺失或多个对应项、speaker 不一致、混合语言、重复 label/old、None-language dialogue、Python/style/条件/原始游戏块、speaker/transition/control 表达式、say 调用参数、空 source、tab 缩进、无法 tokenize 的源文，以及非法属性列表。

后续 golden 应先通过这里的内容契约，再将每个 payload 交给真实 codec，比较完整分类、顺序、中立记录和身份依据。当前契约只核对明确锚点和原始字节，不扫描 RPY 程序或执行任何输入。
