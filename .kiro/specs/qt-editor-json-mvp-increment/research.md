# 研究与设计决策

## 摘要

- **功能**：`qt-editor-json-mvp-increment`
- **Discovery 范围**：现有系统扩展（轻量 brownfield discovery）
- **关键发现**：
  - JSON loader 已把独立 `speaker` 字段写入 frozen `EditorSegment`；本规格中的 raw speaker 是当前模型保存的规范化值，不是从 source 拆分的文本，也不是原文件字节级值。
  - 当前 Controller 已拥有项目会话、导航、dirty 与资源热重载，是 inventory、搜索、预处理和术语 CRUD 的唯一 Qt 接缝；Qt 仍需移除对 `ProjectError` 的直接依赖。
  - `QTextEdit.setPlainText()` 会重置原生撤销栈；逐键撤销、批量预处理撤销和 immutable 项目更新必须分成三个明确边界。
  - 旧术语 CSV 只有两列且现有导入会丢弃扩展列；术语版本化必须使用一个有明确行类型的原子单文件格式，并让所有导入/CRUD 都经过同一 store。

## 研究记录

### Raw speaker 数据边界

- **背景**：用户要求首批批处理主要盘点单 JSON 的 raw speaker。
- **查阅来源**：`editor_contracts.py`、`editor_project.py`、`po/卷一_引.json`。
- **发现**：
  - `EditorSegment` 已包含 `speaker`，项目保存会原样写回该字段。
  - loader 的字符串清理会执行首尾空白规范化，因此运行时 raw speaker 应定义为 `EditorSegment.speaker`。
  - `卷一_引.json` 有 2,942 段；speaker 已是独立字段，不需要解析 `NVLHED "text"`。
- **设计影响**：
  - inventory 是 O(n) 只读扫描，按第一次出现顺序去重并计数。
  - 空 speaker 单独计数；不从 source 猜测、回填或修改项目。
  - speaker profile、别名和头像不进入本设计。

### Controller 与 Qt 接缝

- **背景**：新增能力不能让 Qt 直接访问 store、codec 或 Engine。
- **查阅来源**：`editor_controller.py`、`qt_editor_window.py`、`tests/test_qt_user_journey.py`。
- **发现**：
  - Controller 已通过替换 frozen project/segment 管理 target、confirmed、dirty 和导航。
  - Qt 当前直接导入并捕获 `editor_project.ProjectError`，AST guard 未禁止该依赖。
  - 搜索结果导航可复用 `go_to()`；资源成功变更后的 Trie 重载可复用 `reload_resources()`。
- **设计影响**：
  - 新领域服务保持 Qt 无关，由 Controller 暴露 frozen reports。
  - Controller 统一转换项目、术语和预处理异常；Qt 只捕获 `EditorControllerError`。
  - mutation dialogs 只发 typed reports；主窗口统一从 Controller 刷新 edit、browse、progress 与 suggestions，不接受 dialog 私有副本。
  - AST guard 增加 `editor_project`、新 store 和新领域服务的禁止导入。

### 搜索能力与 Feature 5 合并门

- **背景**：Qt 拥有 Match Case / Whole Word 产品入口，Feature 5 拥有匹配语义。
- **查阅来源**：`.kiro/steering/roadmap.md`、`feature5-ui-integration.md`、Feature 5 Requirements。
- **发现**：
  - 当前代码没有项目搜索接口或统一 `TextMatcher`。
  - 若 Qt 先实现 case-fold/词界，将形成第二套匹配权威。
- **设计影响**：
  - `ProjectSearchService` 只负责遍历 project fields、组装结果和稳定排序，必须注入 Feature 5 的 `TextMatcher` port。
  - 基础搜索使用同一 Core matcher 的固定 `match_case=false, whole_word=false`；Qt 不提供临时 matcher。
  - capability 区分 unavailable、basic validated 和完整 text-v1 validated；Qt MVP 完成门要求 basic 可用，完整 gate 才启用 Match Case/Whole Word 与 configured terms。

### Target-only 预处理与撤销

- **背景**：用户需要受控批量文本处理，同时要求译文框标准撤销/重做。
- **查阅来源**：`editor_controller.py` 的 immutable target 更新、`qt_editor_window.py` 的 `setPlainText()` 与快捷键安装。
- **发现**：
  - 预处理 preview 与 apply 之间可能发生普通编辑，必须检测 stale preview。
  - 程序化 `setPlainText()` 会清空当前 editor undo stack。
  - 项目 dirty 可由当前 frozen project 与保存基线比较获得，无需另存一份 UI dirty 标志。
- **设计影响**：
  - Controller 维护每次打开生成的 project session id、单调 `project_revision` 与 saved project-content digest；preview 同时携带 identity/revision，undo 依据当前 saved baseline 重算 dirty。
  - 只保存最近一次批量应用的项目 identity、应用后 revision 与受影响段落 before/after target/confirmed；新应用覆盖旧撤销点，切换项目清空，后来编辑导致 stale 时拒绝整批撤销。
  - 逐键撤销使用 `QTextEdit` 原生栈；段落切换明确清空，建议插入使用 `QTextCursor` edit block。

### 术语版本化与 CRUD

- **背景**：新记录要保存 Match Case/Whole Word，而旧两列记录不能静默迁移。
- **查阅来源**：`glossary_engine.py`、`resource_importer.py`、`resource_repository.py`。
- **发现**：
  - 旧 Trie 语义是区分大小写的连续子串。
  - 现有 importer/upsert 只保留前两列，不能承担版本化 CRUD。
  - sidecar 会产生双文件提交和删除一致性问题。
- **设计影响**：
  - 采用单文件 mixed CSV：legacy row 始终是两列；v1 row 使用显式 marker、稳定 id、source、target、两个布尔值。
  - legacy policy 的两个 flags 为 `None`，不得显示成新默认值。
  - 新记录默认 `false/true`；Core capability 未通过前记录仍按 legacy preset 参与现有 Trie，只有两个新 flags 暂不参与匹配。
  - importer merge 保持现有 source last-write-wins：覆盖 target 但保留既有 row kind/id/flags，新 source 追加 legacy row。
  - importer 与 CRUD 都经 `TermbaseStore` prepare；同目录 recovery/staged files 先 fsync，Controller 构建 candidate engines 后再 commit。replace 后故障先恢复原字节，成功 commit 只交换预构建引用。

### 视觉与桌面入口

- **发现**：
  - `LocalCAT-logo-silver.png` 已存在，但 launcher 与应用仍引用旧 logo。
  - 资源更多操作列固定为 48 px，没有针对 ellipsis 的内容宽度策略。
- **设计影响**：
  - bootstrap、QApplication、主窗口和对话框统一使用 silver asset。
  - ellipsis 使用 32–40 logical px fixed tool button 与 `ResizeToContents` 操作列，名称/路径列承担 stretch，不随窗口无界增长或遮挡。

## 架构方案评估

| 方案 | 优点 | 风险 / 限制 | 结论 |
|------|------|-------------|------|
| 全部写入 `qt_editor_window.py` | 文件少 | 破坏分层、难做纯逻辑测试、窗口继续膨胀 | 拒绝 |
| 纯领域服务 + Controller 门面 + 薄 Qt | 边界清晰、可测试、保持单会话 | 新增少量模块和 contracts | 采用 |
| Qt 临时实现 case-fold/词界 | 可先显示搜索 | 与 Feature 5 语义漂移 | 拒绝 |
| 术语 flags sidecar | 旧 CSV 不变 | 双文件原子性、身份和删除困难 | 拒绝 |
| mixed versioned CSV | 单资源文件、旧行可原样保留 | parser 必须严格识别行类型 | 采用 |
| 项目级通用 undo stack | 可统一所有操作 | 跨 UI/批处理状态复杂，超出当前范围 | 拒绝 |
| 原生 editor undo + 单批次撤销 | 符合桌面习惯、边界有限 | 段落切换要明确清栈 | 采用 |

## 设计决策

### 决策：四个纯领域服务，不引入第二项目模型

- **选定方案**：Speaker inventory、project search orchestration、target preprocessing 与 termbase store 接收 frozen project/records，返回 frozen reports；Controller 仍持有唯一可变会话。
- **理由**：可独立测试且不让 Qt 或服务复制项目状态。
- **取舍**：Controller 公共接口增加，但依赖方向不变。

### 决策：搜索只消费 Feature 5 matcher

- **选定方案**：Qt 线只定义 project-level request/report 与 capability；字符匹配、原始 offset、case/word semantics 全部由 Core port 返回。
- **理由**：满足用户对两条垂直线合并的裁决。
- **取舍**：搜索执行任务必须排在 Core matcher 可用之后；其他 Qt 增量不受阻塞。

### 决策：术语单文件 mixed CSV

- **选定方案**：
  - legacy：`source,target`
  - v1：`localcat-term-v1,id,source,target,match_case,whole_word`
- **理由**：保留 legacy 行语义，同时让新记录有稳定身份和显式 flags。
- **定位**：v1 使用持久 id；legacy 使用带文件 digest、行 ordinal 和原行摘要的 snapshot locator，可编辑/删除但不会把管理身份写入旧行。
- **取舍**：通用 CSV 工具仍可打开，但列数混合；LocalCAT parser 必须拒绝未知 marker/无效布尔值。

### 决策：批量预处理只保留一个撤销点

- **选定方案**：preview/apply 由 revision 防 stale；undo snapshot 只保留最近成功批次。
- **理由**：满足需求并避免设计项目级命令历史系统。
- **取舍**：再次应用后不能撤销更早批次；应用后若相关段落又被编辑，整批撤销拒绝覆盖新内容，UI 必须明确说明。

## 风险与缓解

- mixed CSV 被旧 importer 截断 — 所有术语导入改为调用 `TermbaseStore.merge_legacy()`，增加 round-trip fixture。
- `QTextEdit` 程序化刷新清空撤销 — 仅切段/换项目允许重置；同段建议用 cursor edit block。
- preview 过期覆盖新编辑 — revision 不一致时拒绝 apply 并要求重新预览。
- Controller 继续泄露 ProjectError — 统一异常并扩展 AST guard。
- 新 dialogs 过度扩张 — 每个对话框只拥有一个交互流程，业务状态仍在 Controller。

## 参考

- [Qt for Python QTextEdit](https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QTextEdit.html)
- [Qt for Python QUndoStack](https://doc.qt.io/qtforpython-6/PySide6/QtGui/QUndoStack.html)
- 本地：`.kiro/specs/qt-editor-mvp/design.md`
- 本地：`.kiro/steering/feature5-ui-integration.md`
