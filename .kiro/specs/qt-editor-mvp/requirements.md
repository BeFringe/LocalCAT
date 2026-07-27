# 需求文档

## 简介

LocalCAT Qt 专业编辑器 MVP 为个人译者提供一个完全本地的桌面翻译工作台。界面借鉴 MateCat 的信息层级与操作节奏：译者在分段双栏编辑区中处理源文和译文，同时查看当前段的翻译记忆与术语建议；语言资源通过齿轮入口集中管理和导入。

本 MVP 复用 LocalCAT 现有精确匹配翻译记忆、Trie 术语提取和本地文件存储能力，重点闭合“打开项目 → 编辑段落 → 使用语言资源 → 确认并保存”的个人翻译流程。

## 边界说明

- **范围内**：本地 JSON/TXT 翻译项目、双栏分段编辑、精确 TM 建议、术语建议、确认译文回写、TMX 记忆库导入、CSV/XLSX 术语表导入、本地资源启停与 Lookup/Update 控制。
- **范围外**：联网服务、账号与共享资源、多人协作、机器翻译、模糊匹配、云端同步、MateCat 服务端兼容以及复杂排版格式回写。
- **相邻期望**：现有 TM 与术语引擎的默认行为和 Excel 适配器必须保持可用；本需求不接管尚未完成的通用 Parser Subsystem。

## 需求

### Requirement 1：专业编辑器工作区

**目标：** 作为个人译者，我希望在一个结构清晰的桌面工作区中看到项目、段落与语言资源，以便持续完成翻译而无需在多个工具之间切换。

#### 验收标准

1. When 用户启动桌面编辑器, the LocalCAT Qt 编辑器 shall 显示可用的主窗口、项目工具栏、段落导航、源文与译文编辑区、语言资源建议区及项目状态栏
2. While 尚未打开项目, the LocalCAT Qt 编辑器 shall 显示可操作的空状态，并提供打开本地文件和载入示例项目的入口
3. When 用户选择一个段落, the LocalCAT Qt 编辑器 shall 将该段设为当前段，并在双栏区域清晰区分只读源文和可编辑译文
4. The LocalCAT Qt 编辑器 shall 使用一致的深蓝标题区、青蓝主操作色、浅色内容卡片和明确的选中状态，保持与参考界面的专业信息层级
5. When 主窗口尺寸改变, the LocalCAT Qt 编辑器 shall 保持编辑区与建议区可读，并允许用户调整主要分栏比例

### Requirement 2：本地项目打开与保存

**目标：** 作为个人译者，我希望打开现有本地文本或双语数据并保存翻译进度，以便在真实项目中使用编辑器。

#### 验收标准

1. When 用户打开包含 `source`、可选 `target` 和可选 `speaker` 字段的 JSON 项目, the LocalCAT Qt 编辑器 shall 按文件顺序创建对应段落并保留已有译文
2. When 用户打开 TXT 项目, the LocalCAT Qt 编辑器 shall 将每个非空文本行创建为一个待翻译段落
3. When 用户保存项目, the LocalCAT Qt 编辑器 shall 将当前段落顺序、源文、译文、说话人和确认状态写入用户选择的本地 JSON 文件
4. If 用户尝试打开无效或不受支持的文件, the LocalCAT Qt 编辑器 shall 显示可理解的错误信息并保留当前项目与未保存编辑
5. While 项目存在未保存更改, when 用户打开其他项目或关闭窗口, the LocalCAT Qt 编辑器 shall 要求用户选择保存、放弃或取消操作
6. When 项目成功打开或保存, the LocalCAT Qt 编辑器 shall 在标题区和状态栏显示文件名及明确的操作结果

### Requirement 3：分段翻译与确认流程

**目标：** 作为个人译者，我希望快速编辑、确认和导航段落，以便形成连续的键盘优先翻译节奏。

#### 验收标准

1. When 用户修改当前译文, the LocalCAT Qt 编辑器 shall 立即在项目模型中保留更改并将项目标记为未保存
2. When 用户确认当前段, the LocalCAT Qt 编辑器 shall 将段落标记为已确认、更新完成进度并移动到下一个未确认段落
3. When 用户使用确认快捷键, the LocalCAT Qt 编辑器 shall 执行与确认按钮相同的行为
4. When 用户在段落导航中选择上一段、下一段或未确认过滤器, the LocalCAT Qt 编辑器 shall 保留当前编辑并显示正确的目标段落
5. While 段落已确认, when 用户再次修改其译文, the LocalCAT Qt 编辑器 shall 将其恢复为待确认状态
6. When 用户确认非空译文, the LocalCAT Qt 编辑器 shall 将源文和译文写入所有启用 Update 的翻译记忆资源

### Requirement 4：翻译记忆建议

**目标：** 作为个人译者，我希望查看和复用本地翻译记忆中的精确匹配，以便减少重复翻译。

#### 验收标准

1. When 当前段发生变化, the LocalCAT Qt 编辑器 shall 查询所有启用 Lookup 的活动翻译记忆资源
2. When 一个或多个资源存在精确匹配, the LocalCAT Qt 编辑器 shall 显示匹配源文、译文、资源名称和 100% 匹配标记
3. When 用户应用一条翻译记忆建议, the LocalCAT Qt 编辑器 shall 将建议译文放入当前译文编辑区但不自动确认该段
4. If 当前段没有精确翻译记忆匹配, the LocalCAT Qt 编辑器 shall 显示明确的无匹配空状态
5. While 某翻译记忆资源未启用、未激活或未勾选 Lookup, the LocalCAT Qt 编辑器 shall 不使用该资源产生建议

### Requirement 5：术语表建议与维护

**目标：** 作为个人译者，我希望在翻译当前段时看到相关术语并能补充术语，以便保持用词一致。

#### 验收标准

1. When 当前段发生变化, the LocalCAT Qt 编辑器 shall 从所有启用 Lookup 的活动术语表中查找源文内出现的术语
2. When 术语命中, the LocalCAT Qt 编辑器 shall 显示源术语、目标术语和所属资源，并在源文区域对命中范围提供可见提示
3. When 用户应用一条术语建议, the LocalCAT Qt 编辑器 shall 将目标术语插入当前译文的光标位置
4. When 用户从编辑器添加有效的源术语与目标术语, the LocalCAT Qt 编辑器 shall 将其写入一个启用 Update 的术语表，并立即用于后续查询
5. If 没有可写术语表或术语输入无效, the LocalCAT Qt 编辑器 shall 说明原因且不写入不完整记录
6. If 当前段没有术语命中, the LocalCAT Qt 编辑器 shall 显示明确的无术语空状态

### Requirement 6：语言资源设置

**目标：** 作为个人译者，我希望通过齿轮入口集中管理本地翻译记忆和术语表，以便控制哪些资源参与查询和回写。

#### 验收标准

1. When 用户点击齿轮按钮, the LocalCAT Qt 编辑器 shall 打开“翻译记忆与术语表”设置界面而不丢失当前编辑
2. The 语言资源设置 shall 按活动与非活动状态列出资源名称、类型、本地路径、Lookup 和 Update 状态
3. When 用户创建新资源, the 语言资源设置 shall 允许用户指定名称和资源类型，并创建可选择的本地空资源
4. When 用户修改活动、Lookup 或 Update 状态, the 语言资源设置 shall 持久保存该状态并让后续查询与回写立即遵循新设置
5. While 资源处于非活动状态, the 语言资源设置 shall 保留资源记录和文件，但不让其参与查询或回写
6. When 用户重新打开设置或重启编辑器, the 语言资源设置 shall 恢复先前保存的资源列表与启用状态

### Requirement 7：翻译记忆与术语表导入

**目标：** 作为个人译者，我希望从设置界面导入已有语言资产，以便复用既有翻译和术语。

#### 验收标准

1. When 用户为翻译记忆资源选择“导入 TMX”, the 语言资源设置 shall 要求选择本地 TMX 文件及源语言和目标语言
2. When TMX 文件包含所选语言对的有效双语单元, the 语言资源设置 shall 将其导入所选资源，并显示已导入、已跳过和错误数量
3. When 用户为术语表资源选择“导入术语表”, the 语言资源设置 shall 接受前两列为源术语和目标术语的 CSV 或 XLSX 文件
4. When 术语表文件包含重复源术语, the 语言资源设置 shall 使用文件中最后一个有效目标术语作为导入结果
5. If 导入文件无效、超过 100 MB、缺少所选语言对或无法读取, the 语言资源设置 shall 显示可操作的错误信息且不破坏目标资源的原有内容
6. When 导入成功, the LocalCAT Qt 编辑器 shall 重新加载对应资源，使新导入内容无需重启即可参与查询

### Requirement 8：本地性、兼容性与可验证性

**目标：** 作为重视隐私的个人译者，我希望编辑器在本地可靠运行且不破坏现有功能，以便安全采用该 MVP。

#### 验收标准

1. The LocalCAT Qt 编辑器 shall 在打开项目、查询资源、确认翻译、导入和保存过程中不发送网络请求
2. The LocalCAT Qt 编辑器 shall 将用户数据与资源配置保存在本地，并以支持中英文内容的编码读取和写入文本数据
3. If 桌面运行依赖缺失, the LocalCAT Qt 编辑器 shall 在启动时提供包含安装方法的明确错误信息
4. While 现有自检与集成验证运行, the LocalCAT Qt 编辑器 shall 不改变既有 TM、术语、逻辑控制器和 Excel 适配器的默认契约
5. When 编辑器在无显示器的测试环境启动, the LocalCAT Qt 编辑器 shall 能够创建主窗口并到达首个可用状态而不发生未处理异常
6. The LocalCAT Qt 编辑器 shall 为打开、保存、确认、上一段、下一段和设置提供可发现的按钮或快捷键
7. When Qt MVP 验收完成, the 项目 README shall 反映当前实际架构、已实现能力、依赖安装、启动方法、验证命令和 `ui-mvp` 分支状态
