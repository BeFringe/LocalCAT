# 需求文档

## 简介

LocalCAT Qt 单 JSON MVP 增量面向在一个本地 JSON 项目中持续工作的个人译者。它先把项目已有的独立 raw speaker 字段盘点清楚并直接显示，再补齐基础关键词搜索、target-only 简易文字预处理、译文框撤销/重做、术语 CRUD、silver logo 和紧凑资源菜单。

本增量不扩展项目格式，不从 source 猜测 speaker，不修改 source，也不改变既有翻译记忆身份。Match Case / Whole Word 的产品控件属于本增量，但只有统一兼容匹配语义完成并通过验收后才能启用；此前必须明确禁用，不得向用户暗示选项已经生效。

## 边界说明

- **范围内**：当前单个 JSON 项目的 raw speaker inventory 与显示、基础 source/target/speaker 搜索、target-only 文字预处理及最近一次批量应用撤销、译文框撤销/重做、本地术语 CRUD 与匹配选项记录、silver logo 和窄 ellipsis。
- **范围外**：新增项目格式、多项目或目录搜索、source 预处理、speaker 推断或拆分、别名与头像、正则或脚本、搜索驱动的 Replace All、模糊翻译记忆、云端和多人协作。
- **相邻期望**：Match Case / Whole Word 的统一语义由 Feature 5 提供；能力合并并验收前，相关控件保持禁用。speaker alias、显式留空和头像属于后续阶段。既有 JSON/TXT、精确翻译记忆、术语建议和 Excel 工作流不得回归。

### Scope Lineage

2026-08-19 已批准的项目搜索表面 amendment 仅扩展现有 Requirement 3：将常驻搜索条收纳为顶栏可折叠入口，增加同时清除 query 与已签发结果的明确操作，并在非空关键词搜索中增加由 `target + confirmed` 派生的“未填写 / 草稿 / 已翻译”段状态筛选。它不改项目格式，不新增 approved/revise 层，不重新纳入本 Spec 已排除的 search-driven Replace/Replace All。

## 需求

### Requirement 1：Raw speaker 批量盘点

**目标：** 作为个人译者，我希望盘点当前 JSON 项目已有的 raw speaker，以便按项目实际顺序了解角色集合和出现频率。

#### 验收标准

1. When 用户对当前单个 JSON 项目执行 speaker 盘点, the LocalCAT Qt 编辑器 shall 扫描每个段落已有的独立 `speaker` 字段
2. When 同一非空 raw speaker 在多个段落中出现, the LocalCAT Qt 编辑器 shall 按其在项目中的首次出现顺序去重并显示总出现次数
3. When 项目包含空 speaker 段落, the LocalCAT Qt 编辑器 shall 将空 speaker 段落数量与非空 raw speaker inventory 分开显示
4. If source 文本看起来包含角色名但独立 `speaker` 字段为空, the LocalCAT Qt 编辑器 shall 不从 source 猜测、拆分或回填 speaker
5. The LocalCAT Qt 编辑器 shall 在 speaker 盘点前后保持 source、target、speaker、confirmed、段落顺序和翻译记忆匹配身份不变
6. When 用户对内容未变化的项目重复执行 speaker 盘点, the LocalCAT Qt 编辑器 shall 返回顺序和计数相同的结果
7. If 当前项目没有任何非空 raw speaker, the LocalCAT Qt 编辑器 shall 显示空 inventory 和项目中的空 speaker 段落总数

### Requirement 2：Raw speaker 编辑与预览显示

**目标：** 作为个人译者，我希望在翻译和浏览时直接看到原始 speaker，以便理解每段文本的说话者上下文。

#### 验收标准

1. When 用户选择一个带非空 speaker 的段落, the LocalCAT Qt 编辑器 shall 在当前段编辑视图中显示该段的 raw speaker
2. When 用户进入浏览或校对预览, the LocalCAT Qt 编辑器 shall 为每个段落显示与编辑视图一致的 raw speaker
3. If 当前段的 speaker 为空, the LocalCAT Qt 编辑器 shall 显示无 speaker 状态且不生成推测性名称
4. While raw speaker 正在显示, the LocalCAT Qt 编辑器 shall 保持原始 speaker、source、target 和翻译记忆匹配身份不变
5. The LocalCAT Qt 编辑器 shall 不在本增量中把 speaker 别名、显式留空配置或头像表示为已可用能力

### Requirement 3：单 JSON 基础关键词搜索

**目标：** 作为个人译者，我希望在当前 JSON 项目中快速查找关键词并逐项导航，以便定位相关 source、target 或 speaker。

#### 验收标准

1. When 用户提交非空关键词, the LocalCAT Qt 编辑器 shall 在当前单个 JSON 项目的 source、target 和 speaker 字段中查找结果
2. The LocalCAT Qt 编辑器 shall 默认使用不区分大小写的连续子串匹配执行基础搜索
3. When 搜索产生一个或多个结果, the LocalCAT Qt 编辑器 shall 按项目段落顺序显示总结果数、命中字段和可识别的文本预览
4. When 用户前往上一个或下一个搜索结果, the LocalCAT Qt 编辑器 shall 定位到对应段落且不丢失当前未保存译文
5. If 用户提交空关键词, the LocalCAT Qt 编辑器 shall 提示输入有效关键词且不改变当前段落
6. If 搜索没有结果, the LocalCAT Qt 编辑器 shall 显示明确的无结果状态且不改变当前段落
7. While 统一兼容匹配能力尚未合并并通过验收, the LocalCAT Qt 编辑器 shall 禁用 Match Case 和 Whole Word 控件并说明它们属于第二阶段
8. While Match Case 和 Whole Word 控件处于禁用状态, the LocalCAT Qt 编辑器 shall 不让控件的视觉状态改变基础搜索结果
9. Where 统一兼容匹配能力已经合并并通过验收, the LocalCAT Qt 编辑器 shall 启用 Match Case 和 Whole Word 控件并让结果遵循已验收的统一语义
10. Where Whole Word 已启用且查询词为纯 CJK 文本, the LocalCAT Qt 编辑器 shall 使用连续文本匹配并返回与未启用 Whole Word 时相同的结果
11. When 项目搜索未被用户召出, the LocalCAT Qt 编辑器 shall 保持搜索面板折叠，并在顶栏提供可发现、可键盘操作的搜索入口
12. When 用户点击搜索入口或使用平台原生 Find 快捷键（macOS `Command+F`，Qt portable text `Ctrl+F`）, the LocalCAT Qt 编辑器 shall 在展开时将焦点置于关键词、在再次触发时折叠；搜索区作为布局行使编辑三栏/校对两栏整体下移，底部导航/确认动作须保持完整可见且位置稳定，由 target 编辑区/浏览列表自适应收矮；编辑/校对视图切换不得自行改变展开状态
13. When 用户执行“清除”, the LocalCAT Qt 编辑器 shall 同时清空关键词、可见结果与 Controller 已签发搜索成员，且不改变当前段、项目内容、dirty 或已选字段/选项
14. When 用户对非空关键词选择段状态, the LocalCAT Qt 编辑器 shall 在调用统一 matcher 前按下列唯一规则筛选段落：未确认且 `target.strip()` 为空是“未填写”，未确认且 target 非空是“草稿”，`confirmed=true` 是“已翻译”
15. The LocalCAT Qt 编辑器 shall 将段状态筛选作为非空关键词搜索的附加条件；只选状态而未输入关键词时仍提示有效输入，不伪造文本字段或命中 offset
16. If 已签发状态搜索所依赖的 target、confirmed、字段选择或状态筛选后来改变, the LocalCAT Qt 编辑器 shall 使旧搜索结果失效并拒绝使用其导航

### Requirement 4：Target-only 简易文字预处理

**目标：** 作为个人译者，我希望预览并显式应用有顺序的简单文字规则，以便批量整理译文且不会误改原文或项目身份。

#### 验收标准

1. When 用户配置一条或多条已启用的普通文字规则, the LocalCAT Qt 编辑器 shall 按用户可见的规则顺序计算当前项目 target 的预处理结果
2. When 用户请求预览, the LocalCAT Qt 编辑器 shall 显示受影响段落数以及每个受影响段落的修改前后内容
3. While 预览尚未被用户明确应用, the LocalCAT Qt 编辑器 shall 保持项目内容和确认状态不变
4. When 用户明确应用预览结果, the LocalCAT Qt 编辑器 shall 只修改实际命中规则的 target 并将项目标记为未保存
5. When 已确认段落的 target 因批量预处理发生变化, the LocalCAT Qt 编辑器 shall 将该段恢复为待确认状态
6. When 段落 target 未因批量预处理发生变化, the LocalCAT Qt 编辑器 shall 保持该段原有 confirmed 状态
7. The LocalCAT Qt 编辑器 shall 在文字预处理过程中保持 source、raw speaker、段落身份、段落顺序和翻译记忆匹配身份不变
8. When 用户取消预览, the LocalCAT Qt 编辑器 shall 丢弃本次预览且不修改项目
9. If 规则无效或没有可应用的实际变化, the LocalCAT Qt 编辑器 shall 说明原因且不创建虚假的项目修改
10. The LocalCAT Qt 编辑器 shall 不在打开项目或编辑过程中自动运行文字预处理
11. The LocalCAT Qt 编辑器 shall 不把正则、脚本或搜索驱动的 Replace All 表示为本增量可用能力

### Requirement 5：撤销最近一次批量预处理

**目标：** 作为个人译者，我希望显式撤销最近一次批量预处理，以便在发现批量结果不合适时恢复应用前状态。

#### 验收标准

1. When 一次批量预处理成功应用, the LocalCAT Qt 编辑器 shall 提供可发现的“撤销最近一次应用”操作
2. When 用户撤销最近一次批量预处理, the LocalCAT Qt 编辑器 shall 恢复该批次涉及段落在应用前的 target 和 confirmed 状态
3. When 用户成功应用新的批量预处理, the LocalCAT Qt 编辑器 shall 以新批次替换上一批次的撤销点
4. If 当前项目没有可撤销的批量预处理, the LocalCAT Qt 编辑器 shall 说明没有可撤销内容且不修改项目
5. When 用户切换或关闭当前项目, the LocalCAT Qt 编辑器 shall 不把上一项目的批量撤销点用于其他项目
6. While 执行批量撤销, the LocalCAT Qt 编辑器 shall 保持 source、raw speaker、段落身份、段落顺序和翻译记忆匹配身份不变

### Requirement 6：译文框本地撤销与重做

**目标：** 作为个人译者，我希望使用常见快捷键撤销或重做译文编辑，以便安全修正逐次输入。

#### 验收标准

1. While 输入焦点位于译文框, when 用户按下 `Ctrl+Z`, the LocalCAT Qt 编辑器 shall 撤销该译文框最近一次可撤销的文本编辑
2. While 输入焦点位于译文框, when 用户按下 `Ctrl+Y`, the LocalCAT Qt 编辑器 shall 重做该译文框最近一次已撤销的文本编辑
3. While 输入焦点位于译文框, when 用户按下 `Ctrl+Shift+Z`, the LocalCAT Qt 编辑器 shall 执行与 `Ctrl+Y` 相同的重做行为
4. When 撤销或重做改变可见译文, the LocalCAT Qt 编辑器 shall 让当前会话 target 与可见文本保持一致
5. When 用户首次修改一个已确认段落的译文, the LocalCAT Qt 编辑器 shall 将该段恢复为待确认状态
6. If 当前译文框没有可撤销或可重做的文本编辑, the LocalCAT Qt 编辑器 shall 保持当前译文不变

### Requirement 7：本地术语 CRUD 与匹配兼容性

**目标：** 作为个人译者，我希望集中查看并维护本地术语，同时保留旧术语行为，以便安全改善当前项目的用词一致性。

#### 验收标准

1. When 用户打开本地术语管理入口, the LocalCAT Qt 编辑器 shall 显示可管理术语的 source、target 和匹配选项状态
2. When 用户提交有效的新术语, the LocalCAT Qt 编辑器 shall 保存该术语并让当前段后续建议立即反映新增记录
3. When 用户修改现有术语, the LocalCAT Qt 编辑器 shall 保存修改并让当前段后续建议立即反映最新记录
4. When 用户确认删除现有术语, the LocalCAT Qt 编辑器 shall 删除该术语并让当前段后续建议不再返回该记录
5. If 新增、修改或删除发生无效输入、重复或冲突, the LocalCAT Qt 编辑器 shall 说明原因且不产生不完整记录
6. If 术语变更无法完整保存, the LocalCAT Qt 编辑器 shall 保留变更前可用记录并显示可操作错误
7. When 用户创建新术语记录, the LocalCAT Qt 编辑器 shall 将其默认设置为 `Match Case=false` 和 `Whole Word=true`
8. While 统一兼容匹配能力尚未合并并通过验收, the LocalCAT Qt 编辑器 shall 禁用术语的 Match Case 和 Whole Word 控件并明确说明设置尚未参与匹配
9. While 旧两列术语记录尚未被用户明确迁移, the LocalCAT Qt 编辑器 shall 保持其既有区分大小写与子串匹配行为
10. The LocalCAT Qt 编辑器 shall 不把旧两列术语记录静默改写为新记录默认值
11. Where 统一兼容匹配能力已经合并并通过验收, the LocalCAT Qt 编辑器 shall 按每条新术语记录保存的 Match Case 和 Whole Word 设置执行匹配
12. Where Whole Word 已启用且术语 source 为纯 CJK 文本, the LocalCAT Qt 编辑器 shall 使用连续文本匹配并返回与未启用 Whole Word 时相同的结果
13. When 用户重新打开术语管理入口或重启编辑器, the LocalCAT Qt 编辑器 shall 恢复此前成功保存的术语变更和新记录匹配选项
14. When 用户从主窗口 Termbase 页或语言资源设置访问“管理术语”, the LocalCAT Qt 编辑器 shall 让两个入口打开同一个集中式术语管理能力，并只列出当前 Active+Update 的术语表

### Requirement 8：Silver logo、紧凑资源操作与平台快捷键

**目标：** 作为桌面用户，我希望应用图标与资源操作保持一致且紧凑，以便快速识别 LocalCAT 并清楚使用资源菜单。

#### 验收标准

1. When 用户从桌面入口查看或启动 LocalCAT, the LocalCAT Qt 编辑器 shall 使用 `LocalCAT-logo-silver.png` 作为应用图标
2. When LocalCAT 主窗口或对话框显示, the LocalCAT Qt 编辑器 shall 使用与桌面入口一致的 silver logo
3. When 资源列表显示 ellipsis 更多按钮, the LocalCAT Qt 编辑器 shall 让按钮宽度与其内容和可操作范围相称且不占用多余列表空间
4. When 用户使用指针或键盘访问 ellipsis 按钮, the LocalCAT Qt 编辑器 shall 提供可识别的更多操作说明并打开对应资源菜单
5. When 资源设置窗口调整尺寸, the LocalCAT Qt 编辑器 shall 保持 ellipsis 可操作且不遮挡相邻关键信息
6. When 用户在 macOS 使用实体 `Control+Tab` 或 `Control+Shift+Tab`, the LocalCAT Qt 编辑器 shall 切换 Translation Matches 与 Termbase，且不得注册会与系统应用切换冲突的 `Command+Tab`
7. When 用户在 macOS 使用编辑/校对模式快捷键, the LocalCAT Qt 编辑器 shall 继续以 `Command+1` / `Command+2` 切换；其他平台继续使用其 Qt 原生主修饰键映射
8. When 用户展开编辑/校对模式下拉项, the LocalCAT Qt 编辑器 shall 将 popup 完整放在顶栏控件下方，不覆盖当前“编辑”文字或触发区
9. When 用户使用平台原生主修饰键 + `Shift+L`（macOS `Command+Shift+L`）, the LocalCAT Qt 编辑器 shall 在“紧凑”与“自动换行”两种段落显示密度之间切换，且工具提示显示原生快捷键

### Requirement 9：单 JSON 边界、兼容性与本地性

**目标：** 作为现有 LocalCAT 用户，我希望本增量保持本地、可恢复且不破坏已有工作流，以便安全采用新增能力。

#### 验收标准

1. The LocalCAT Qt 编辑器 shall 仅对当前打开的单个 JSON 项目承诺本增量的 speaker 盘点、搜索和预处理行为
2. The LocalCAT Qt 编辑器 shall 不因本增量新增 PO、RPY、XLIFF、多文件夹 JSON 或多项目操作入口
3. While 用户使用本增量能力, the LocalCAT Qt 编辑器 shall 不发送项目、speaker、搜索、预处理或术语数据到网络
4. If speaker 盘点、搜索、预处理或术语操作失败, the LocalCAT Qt 编辑器 shall 保留失败前可用的项目与资源状态并显示可理解错误
5. When 任一新操作改变当前项目或术语, the LocalCAT Qt 编辑器 shall 让编辑、浏览、进度和建议中的可见状态保持一致
6. The LocalCAT Qt 编辑器 shall 保持既有精确翻译记忆优先级、raw speaker 翻译记忆身份、术语建议和 Excel 三态输出不变
7. The LocalCAT Qt 编辑器 shall 保持既有 JSON/TXT 打开能力和 JSON 保存结果可用，但不把 TXT 宣称为本增量的新能力范围
