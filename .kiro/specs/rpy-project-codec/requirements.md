# 需求文档

## 简介

LocalCAT 为 Ren'Py TL 译者提供独立的格式支持：打开翻译模板，在现有编辑器处理源文、speaker、译文和确认状态，以 ProjectPackage 持久化，并导出可复核的 TL。首个交付先完成单个 TL，再扩展至显式选择的多个 TL。

## 边界说明

- **范围内**：声明支持的 TL dialogue/string 子集、保真导出、单/多文档项目包、源变更调和及中立 UI。
- **范围外**：任意游戏源码执行、codec 自行扫描目录或后台自动收录文件、游戏资源打包、网络同步、第三方插件下载、TM/术语内部存储与检索算法。
- **相邻期望**：Project 保有身份/保存/恢复权威；Parser 保有中立 codec 接缝；语言资源使用既有 TM JSONL 与术语 CSV ResourcePackage。

### Scope Lineage

- **Owning spec**：`rpy-project-codec`。
- **被修订的既有范围说明**：Multi Requirements 3.5 的 RPY 延期范围；Multi Design 的 single-file origin 和未来 source writer 接缝；Parser 中立 source-round-trip capability 的执行扩展。
- **相邻规格 / 契约**：`parser-subsystem-extraction`、`multi-document-project-workspace`、`qt-editor-json-mvp-increment`、`windows-platform-enablement`。
- **范围差异**：单 TL 采用非 legacy ProjectPackage profile，目录选择由 Multi 提供；Parser 提供中立 round-trip 准备/发布，RPY 手工包闭环先于 Sync。 具体 owner 合同与执行依赖见 [跨规格合同](cross-spec-amendments.md)。

## 需求

### Requirement 1：独立、可配置的 TL 输入

**目标：** 作为译者，我希望明确知道哪些 TL 可以打开，以免错误解析脚本。

#### 验收标准
1. When 用户选择单个 `.rpy` TL, the LocalCAT shall 使用已启用且兼容的 codec 验证整个文档，仅在验证完成后提供可编辑项目。
2. If 输入属于未支持语法、损坏编码、歧义源文/译文对应关系或混合目标语言, the LocalCAT shall 给出文件位置和原因，并保留当前项目及源文件。
3. If codec 缺失、禁用或不兼容, the LocalCAT shall 显示明确的不可用原因，不以 TXT 或其他格式猜测读取/写回。
4. The LocalCAT shall 从自身发行内容和本机配置提供该能力，运行和测试均不要求其他项目的目录、脚本或 Ren'Py 安装。

### Requirement 2：翻译槽、中立编辑与稳定身份

**目标：** 作为译者，我希望准确编辑每个翻译槽，并保留已有译文。

#### 验收标准
1. When 读取受支持的 dialogue block 或 old/new string pair, the LocalCAT shall 显示准确的源文、已有或空译文及可识别的 raw speaker；同一文件可同时包含两种形式，say 的角色标识符或姓名字符串作为 speaker，姓名中的 interpolation 原样保留且不求值；显示属性与 transition 不并入 speaker 或可翻译文本，控制行不成为翻译段落。
2. When 源输入包含空译文、相同显示文本或文本内标签, the LocalCAT shall 保留各翻译槽及其顺序，不过滤空槽、不合并不同身份的段落。
3. When 用户修改并保存 target 或 confirmed, the LocalCAT shall 在项目中保存精确编辑状态；首次导入的译文默认未确认，不依据非空文本自动确认。
4. If 身份重复或无法唯一关联, the LocalCAT shall 阻止歧义导入/回填并指出冲突，不使用界面行号、basename 或修改时间猜测对应关系。

### Requirement 3：单 Document ProjectPackage

**目标：** 作为译者，我希望一个项目包完整保存单个 TL 的翻译工作。

#### 验收标准
1. When 用户在桌面打开单 TL, the LocalCAT shall 先完整验证输入，再引导建立一个 Document 的 ProjectPackage；只有包持久化成功后才进入该项目编辑，包保存原始 TL、保真所需私有数据及中立编辑状态，不要求另存 JSON/sidecar 文件对。首次进入的保存基线为干净状态，实际编辑后才显示未保存修改。
2. When 用户冷重开该包, the LocalCAT shall 恢复段落身份、target、confirmed 和顺序，并在兼容 codec 可用时允许从包内原始内容导出，无须找回原路径。
3. If 打开包时 codec 不可用, the LocalCAT shall 允许既有合同支持的中立编辑与包保存，明确禁用 TL 导出，原样保留私有成员。
4. If 包保存或恢复失败, the LocalCAT shall 沿用 Project 的保存报告、dirty/baseline 和恢复行为，不把未确认发布的状态显示为保存成功；导入阶段取消建包或建包失败时保留原项目，已经发生的发布按实际结果报告。

### Requirement 4：预览与保真导出

**目标：** 作为译者，我希望导出的 TL 只包含预期修改，且失败不会静默损坏已有文件。

#### 验收标准
1. When 用户请求导出, the LocalCAT shall 先显示目标、修改段数、空译文与未确认译文数量，以及阻断诊断；默认导出全部当前 target，空译文不回退为源文，confirmed 不写入 TL。
2. When 项目未修改任何翻译文本, the LocalCAT shall 导出与原 TL 完全相同的字节，包括 BOM、换行、缩进、注释和控制行。
3. When target 有修改, the LocalCAT shall 仅改写相应译文字符串的内容，保留源文、speaker、say 属性、transition 及非翻译跨度，并正确处理引号、反斜杠、换行、interpolation 和 text tags。
4. If 占位符/标签校验不通过、预览已过期、目标被替换或 codec 身份不匹配, the LocalCAT shall 拒绝发布并提供定位诊断，不产生部分成功文件。
5. When 发布结果不能确定, the LocalCAT shall 显示不确定/需检查结果，不签发虚假成功或承诺已恢复旧文件；导出不会替代项目包保存。

### Requirement 5：多文档选择与源更新

**目标：** 作为译者，我希望多个章节复用同一个项目模型，而不会发生文件名碰撞。

#### 验收标准
1. When 用户通过 Project 的 TL 根目录递归预览勾选文件，或使用既有显式文件选择入口, the LocalCAT shall 仅解析确认选择的受支持 TL，按 review 确认顺序建立 Document，保留相对于原选择根目录的路径，并区分嵌套目录下的同名文件；未勾选内容不得读取或纳入项目。
2. When 用户切换章节、搜索、保存或重开, the LocalCAT shall 沿用既有复合段落身份、文档顺序、当前章节/全部章节范围与项目持久化行为；文件夹导航按相对目录分组且不改变阅读顺序或身份。
3. When 用户明确重新绑定并调和源更新, the LocalCAT shall 报告 unchanged/source_changed/new/removed/ambiguous，保留 changed 段的已有 target 并撤销 confirmed，rename 只消费显式映射。
4. When 用户批量导出多个 TL, the LocalCAT shall 在用户选择的导出根目录下按各 source_ref 保留嵌套路径，先验证全部候选及路径冲突，再报告逐文件结果；失败或取消时明确已发布与未发布文件，不宣称跨文件原子性。

### Requirement 6：编辑器与资源消费

**目标：** 作为译者，我希望 RPY 项目自然地使用 LocalCAT 的现有编辑和语言资源能力。

#### 验收标准
1. When 用户进入 RPY 项目, the LocalCAT shall 使用现有源文/译文/speaker、编辑/浏览、搜索和确认交互，不向用户暴露 token、私有成员或内部 codec 类型。单 Document 项目不重复显示章节栏和分隔行，需要区分来源时使用实际文件名；切换项目后所有提示均来自当前项目。
2. When 用户使用 TM 或术语建议, the LocalCAT shall 继续使用既有语言资源的显式启用、查询、应用与更新行为，不把 TMX 资源包导入设为 RPY 前置。
3. While 导入、预览或导出正在运行, the LocalCAT shall 保持 UI 可响应、允许取消，并拒绝过期结果覆盖后来编辑或已关闭的项目。
4. When 用户手工把项目包与选定的 JSONL/CSV 资源包交给另一设备, the LocalCAT shall 分别重新验证并按各自事务导入，不运输设备资格或恢复项目包内的执行权限。

### Requirement 7：有界与可验证的失败保护

**目标：** 作为译者，我希望异常输入和中断不会破坏其他项目或本地状态。

#### 验收标准
1. If 输入、私有映射或输出超过声明的限制, the LocalCAT shall 在发布前拒绝，并报告超限维度，不耗尽无界内存或磁盘。
2. If 读取被截断、取消、源身份变化或处理者失败, the LocalCAT shall 丢弃未完成候选，不发布部分项目。
3. The LocalCAT shall 不执行 TL 中的代码、表达式或 interpolation，不从包中取得可执行插件。
4. When 完成该能力的验收, the LocalCAT shall 以仓库内可重现的单文件/多文件、保真、故障和手工包旅程证明声明范围，不依赖个人外部目录或历史测试计数。
