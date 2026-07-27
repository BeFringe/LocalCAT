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
