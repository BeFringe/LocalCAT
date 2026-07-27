# 研究与设计决策

## 摘要

- **功能**：`qt-editor-mvp`
- **Discovery 范围**：现有系统扩展（轻量 brownfield discovery）
- **关键发现**：
  - 现有 `LogicController.get_suggestions()` 的三态响应与 TM 优先规则已被 Excel 适配器使用，Qt MVP 必须旁路扩展而不能破坏该契约。
  - 现有引擎可提供精确 TM 查询、追加式 TM 写入与 Trie 术语查询，但缺少编辑会话、资源注册、TMX 导入和术语持久化接口。
  - MateCat 的核心价值不在复刻全部在线功能，而在闭合“当前段编辑 → TM/术语建议 → 确认写回 → 资源设置”的节奏。

## 研究记录

### 现有 LocalCAT 扩展点

- **背景**：Qt UI 必须建立在 `feature/logic-excel-adapter` 的现有层次上。
- **查阅来源**：`logic_controller.py`、`tm_engine.py`、`glossary_engine.py`、`excel_adapter.py`、`excel_adapter_openpyxl.py`、`.kiro/steering/*.md`。
- **发现**：
  - `TMEngine` 按 source 建立精确索引，重复项采用后写胜出；`save_record()` 追加 JSONL。
  - `GlossaryEngine` 返回所有重叠术语命中；`GlossaryLoader` 可读取两列 CSV/XLSX，但运行时 `add_term()` 不负责持久化。
  - `LogicController` 固定加载默认 `tm.jsonl` 与 `terms.csv`，TM 命中时提前返回，因此不能直接支持并列 TM/术语页签和多资源。
  - 现有 Parser Subsystem 仍是未实施规格，本功能不能假定其已存在，也不接管通用 PO/JSON/TMX 解析体系。
- **设计影响**：
  - 保留旧 `LogicController` 外部行为。
  - 增加独立 `EditorController`，让 Qt 前端仍只调用 Logic 层。
  - 把资源解析和原子写入保持为 Qt 无关模块，未来 Parser Subsystem 落地时可替换其实现。

### 遗留 MCA Playbook 的权威级别

- **背景**：`plugins/modular-cat-architect/SKILL.md` 与 main 分支早期 README 同期形成，其阶段编号和 Logic 状态职责已不能完整描述当前分支。
- **发现**：
  - 该 playbook 把 PySide6 写作 Phase 3，并要求旧 Logic UI 维护 Session；当前 `feature/logic-excel-adapter`、最新 steering 和实际代码已经把 `LogicController` 定义为无请求历史的 TM 优先转发器。
  - 当前工作区 README 已更新 Feature 3 的发布状态，但 Feature 4、运行依赖、测试和最新文件结构仍未反映 Qt MVP。
- **设计影响**：
  - 权威顺序固定为：当前用户要求 → `.kiro/steering/` → 当前分支可运行代码与契约 → 当前规格；遗留 MCA 只保留为历史意图参考。
  - 不以 MCA 的旧阶段编号或旧 README 推导实现边界。
  - README 在验收后更新，并保留用户已经修改的 Feature 3 标签与推送状态。

### MateCat 资源与编辑工作流

- **背景**：用户要求参考 MateCat，并特别指定齿轮设置中的语言资源导入流程。
- **查阅来源**：
  - [Translation Memory and Termbase](https://guides.matecat.com/activ)
  - [Create a private translation memory](https://guides.matecat.com/importing/exporting)
  - [How to import/export a termbase](https://guides.matecat.com/how-to-add-a-termbase)
  - [Edit your translation](https://guides.matecat.com/translate-1)
  - [MateCat repository](https://github.com/matecat/MateCat)
- **发现**：
  - 活动资源通过 Lookup/Update 控制查询与写回；确认段落会写入允许 Update 的 TM。
  - 编辑器围绕源文、译文、翻译匹配和术语表构成主循环，`Ctrl+Enter` 是确认段落的关键快捷键。
  - 官方导入页同时出现 100 MB 和 300 MB 两种上限，不能把该冲突当作 LocalCAT 契约。
- **设计影响**：
  - LocalCAT 明确采用 100 MB 本地上限并在 UI 展示。
  - 只学习信息架构和操作节奏，不复制 MateCat 标识、样式源码或在线协作功能。

### TMX 与不可信 XML

- **背景**：现有代码不支持 TMX，而用户明确要求在设置中导入记忆库。
- **查阅来源**：
  - [TMX 1.4b specification](https://www.ttt.org/oscarStandards/tmx/tmx14b.html)
  - [Python XML security](https://docs.python.org/3/library/xml.html)
- **发现**：
  - 一个 `<tu>` 可含多个带 `xml:lang` 的 `<tuv>`；MVP 必须由用户选择源/目标语言。
  - Level 2 行内标记无法无损映射到当前纯字符串 TM。
  - XML 的 DTD、实体与资源耗尽风险需要在边界上阻断。
- **设计影响**：
  - 使用流式解析；拒绝 DTD/ENTITY；限制文件大小。
  - 只导入无行内元素的 Level 1 `<seg>`；缺语言对或带标签单元计入跳过/错误。
  - 完整解析和校验成功后再原子替换目标 JSONL，避免损坏已有资源。

### PySide6 兼容性

- **背景**：当前环境为 Python 3.14.6 且尚未安装 PySide6。
- **查阅来源**：
  - [Qt for Python](https://doc.qt.io/qtforpython-6/)
  - [PySide6 on PyPI](https://pypi.org/project/PySide6/)
  - [QFileDialog](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QFileDialog.html)
  - [QSettings](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QSettings.html)
  - [QTest](https://doc.qt.io/qtforpython-6/PySide6/QtTest/QTest.html)
- **发现**：
  - PySide6 6.11.1 声明支持 Python 3.10 至 3.14。
  - Qt Widgets 足以实现桌面 MVP；无需引入 QML 或 SQLite。
  - `QT_QPA_PLATFORM=offscreen` 可用于无显示环境的 GUI 冒烟。
- **设计影响**：
  - `requirements-ui.txt` 固定经过验证的 PySide6 6.11.1，并保留 `openpyxl` 作为 XLSX 读取依赖。
  - 启动入口延迟导入 Qt，在依赖缺失时给出明确安装指令。
  - `QSettings` 只保存窗口与交互偏好，语言资源清单由可测试的 Qt 无关仓库保存。

### 真实项目冒烟反馈与 UI 数据边界

- **背景**：用户使用 `po/卷一_引.json`、设置新建资源和长篇段落列表完成首轮人工冒烟。
- **本地证据**：
  - PySide6 6.11.1 的 `QComboBox.addItem(..., ResourceKind.TRANSLATION_MEMORY)` 经 QVariant 往返后，`currentData()` 返回普通字符串 `"translation_memory"`，不是 `ResourceKind` 实例。
  - `ResourceRepository.create_resource()` 对 `isinstance(kind, ResourceKind)` 的严格判断因此拒绝来自真实 UI 的有效选择；测试此前只直接传 Enum，未覆盖 QVariant 边界。
  - `RpySeriesExtract/OWNattempt.tmx` 使用 `en-US → zh-CN`，现有安全导入器可导入 165 条、跳过 67 条；对 `卷一_引.json` 的 2942 段产生 112 个精确命中。
  - 设置表只有路径列使用 Stretch，类型列固定、导入列 ResizeToContents，样式内边距下仍会发生中文文字裁切，窗口增宽也不会分配空间给名称列。
- **设计影响**：
  - 资源类型在 Repository 的公开输入边界从 Enum 或受支持字符串归一化为 Enum，未知值仍然拒绝。
  - 设置表把名称和路径作为弹性列，类型与导入列按完整内容给出最小宽度。
  - 新增真实 QVariant 回归和真实 `OWNattempt.tmx + 卷一_引.json` 验收，防止只验证构造器直调。

### 长篇项目恢复与浏览校对模式

- **背景**：2942 段项目暴露了单一当前段编辑视图在进度恢复和全文校对方面的不足。
- **方案评估**：
  - 仅把左栏改成自动换行会改善可读性，但不能同时查看双语上下文，不足以形成浏览/校对流程。
  - 直接在同一段落列表塞入源文和译文会破坏紧凑导航密度，也会让三栏编辑区过窄。
  - 独立只读双语浏览页可复用当前会话，在全文浏览与精确编辑之间保留同一段落索引。
- **设计影响**：
  - 左栏提供“紧凑等高”和“自动换行”两个密度选项。
  - 工作区提供“编辑”和“浏览/校对”模式；浏览页展示双语全文，双击回到同段编辑。
  - 最近项目、最后段落和显示偏好保存到独立本地 `workspace.json`，不改写用户源项目或语言资源清单。
  - Linux 提供 stdlib 桌面启动入口安装命令；应用运行仍保持无网络和无服务端依赖。

## 架构方案评估

| 方案 | 描述 | 优点 | 风险 / 限制 | 结论 |
|------|------|------|---------------|------|
| 直接在 QWidget 调引擎 | UI 直接实例化 TM/Glossary | 文件少 | 违反四层依赖，难测试，多资源逻辑泄漏到 UI | 拒绝 |
| 修改旧 LogicController | 把编辑状态和资源管理塞入旧类 | 表面复用 | 破坏无状态三态契约与 Excel 回归 | 拒绝 |
| 新 EditorController | 新 Logic 层协调编辑会话、资源与引擎 | 保持旧契约，Qt 可薄化，便于测试 | 新增少量模块 | 采用 |
| 立即使用 SQLite | TM/术语统一数据库 | 未来查询空间大 | 改变存储契约，超出精确匹配 MVP | 拒绝 |
| JSON 清单 + JSONL/CSV 资源 | 元数据 JSON，数据继续使用现有格式 | 兼容、可审计、迁移简单 | 大规模模糊查询仍有限 | 采用 |

## 设计决策

### 决策：编辑会话与旧逻辑控制器分离

- **背景**：MVP 需要可变编辑状态，但旧 `LogicController` 被定义为无请求历史的转发器。
- **选定方案**：新建 `EditorController` 管理 `EditorSession`，旧 `LogicController.get_suggestions()` 的输出和默认路径不变。
- **理由**：满足 Qt 的状态需求，同时保护 Excel 与 benchmark 的既有接缝。
- **取舍**：两套 Logic 入口短期并存；未来可在不影响本 MVP 的情况下提炼共享查询服务。

### 决策：资源操作采用事务式文件替换

- **背景**：导入失败不能损坏已有 TM 或术语表。
- **选定方案**：在内存中完成解析与合并，把完整结果写入同目录临时文件，刷新并 `os.replace()`。
- **理由**：在当前本地单进程 MVP 中提供简单可靠的原子性。
- **取舍**：合并阶段仍需建立索引，不适合作为未来超大 TM 的最终方案。

### 决策：只支持真实可验证的格式

- **选定方案**：
  - 项目：JSON 与逐行 TXT。
  - TM：TMX 1.4/1.4b Level 1 纯文本。
  - 术语：两列 CSV 与 XLSX。
- **理由**：现有 `openpyxl` 不支持旧 `.xls`，通用 Parser Subsystem 尚未实现。
- **取舍**：PO、XLIFF、ODS、旧 XLS 与带标签 TMX 留待后续规格。

### 决策：复刻工作流而非网页像素

- **选定方案**：深蓝顶栏、双栏当前段、段落导航、TM/术语页签、青蓝确认按钮和齿轮设置对话框。
- **理由**：保留用户熟悉的主循环，同时适配桌面窗口、键盘和可调整分栏。
- **取舍**：不放置 QR、QA、聊天、共享、罚分等无实现按钮。

### 决策：工作区状态与翻译项目分离

- **背景**：最近项目、最后段落和显示偏好属于本机工作习惯，不应写进可交换的翻译项目。
- **选定方案**：新增原子 JSON 工作区状态仓库，由 EditorController 协调项目最近记录与段落断点；Qt 只通过控制器读写。
- **理由**：保持项目文件可移植并延续 Layer 4 → Layer 3 → Storage 的依赖方向。
- **取舍**：工作区状态是单机配置，不跨设备同步。

### 决策：编辑模式与浏览校对模式共享一个会话

- **背景**：用户需要 MateCat 式全文双语浏览，同时保留 LocalCAT 三栏编辑节奏。
- **选定方案**：主窗口内使用工作区页面切换；浏览页只读展示当前 EditorProject，双击段落切回编辑页。
- **理由**：不复制第二份项目模型，切换不会丢失未保存译文或确认状态。
- **取舍**：MVP 的浏览校对页不直接编辑单元格，修改通过双击返回专业编辑区完成。

## 风险与缓解

- PySide6 安装体积较大 — 独立 `requirements-ui.txt`，不影响核心模块运行。
- 100 MB 导入可能阻塞主线程 — 设置页通过 worker 线程执行导入并禁用冲突操作。
- 现有 loader 吞异常 — 新资源路径使用结构化 `ImportReport` 与显式异常，不依赖 stdout。
- 资源文件被外部修改 — 每次打开设置或导入后重载引擎，读取失败时保持旧运行实例并向用户报告。
- 当前 `logic_controller.py` 自检夹具漂移 — 只修正夹具句子，不改变三态业务契约。
- README 存在用户未提交更新 — 在原有 diff 上增量修改，保留 Feature 3 标签语义和已推送状态。

## 参考

- [MateCat language assets guide](https://guides.matecat.com/language-assets)
- [MateCat LGPL repository](https://github.com/matecat/MateCat)
- [Qt for Python documentation](https://doc.qt.io/qtforpython-6/)
- [PySide6 package metadata](https://pypi.org/project/PySide6/)
