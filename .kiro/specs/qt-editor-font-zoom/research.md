# 调研与设计决策

## Summary

- **Feature**: `qt-editor-font-zoom`
- **Discovery Scope**: Extension / Simple Addition
- **Key Findings**:
  - 现有 `DisplayPreferences`、`EditorController.update_display_preferences()` 和 `WorkspaceStateRepository` 已形成 Qt 无关的本地显示偏好闭环，可直接扩展字号字段。
  - source/target 当前同时受 Qt 样式表固定 `15px` 及 source HTML 内联 `font-size:15px` 约束；只调用 `QWidget.setFont()` 不能保证视觉生效。
  - 滚轮事件实际到达 `QTextBrowser`/`QTextEdit` 的 viewport；在两个 viewport 上安装范围受限的 event filter，可以满足“区域外不缩放”和普通滚轮不变。

## Research Log

### 本地显示偏好扩展点

- **Context**: 字号需要像最近项目和显示模式一样在本机恢复，且不得进入项目文件。
- **Sources Consulted**:
  - `editor_contracts.py`
  - `workspace_state.py`
  - `editor_controller.py`
  - `tests/test_workspace_state.py`
- **Findings**:
  - `DisplayPreferences` 是 frozen dataclass，目前持有段落密度与工作区模式。
  - `workspace.json` 的 `display` 对象由 `WorkspaceStateRepository` 原子读写。
  - Qt 只能通过 `EditorController` 获取和更新显示偏好，符合现有 Layer 4 → Layer 3 → Layer 1 依赖方向。
- **Implications**:
  - 在 `DisplayPreferences` 增加有界整数即可，不需要新的 repository 或 Qt 直接文件访问。
  - 新字段保持可选读取，旧 `schema_version=1` 文件无需迁移。

### Qt 字体覆盖与事件边界

- **Context**: Ctrl+滚轮必须同步改变 source/target，普通滚轮和其他控件不得受影响。
- **Sources Consulted**:
  - `qt_editor_window.py` 的 `render_highlighted_source()`、`_build_edit_panel()`、`refresh_suggestions()` 和应用样式表
  - PySide6 6.11.1 已有 `QEvent`、`QWheelEvent`、`QFont` 和 QObject event filter 能力
- **Findings**:
  - source 使用 `QTextBrowser`，target 使用 `QTextEdit`；滚轮输入由各自 viewport 接收。
  - source 高亮 HTML 和控件 QSS 都固定了 `15px`，会覆盖运行时字体。
  - 两个文本控件均可通过 widget font 与 document default font 使用同一逻辑像素字号。
- **Implications**:
  - 移除仅针对 source/target 的固定字号声明，保留颜色、边框、间距与字重。
  - event filter 只安装在两个 viewport；无 Ctrl 时不消费事件，有 Ctrl 时按滚轮方向执行一个有界步进。

### 保存失败语义

- **Context**: 偏好保存失败时，需求要求保持当前可见字号，同时不破坏此前有效状态。
- **Sources Consulted**:
  - `EditorController.update_display_preferences()`
  - `WorkspaceStateRepository._write_state()`
  - `QtEditorWindow.set_segment_density()` 的现有错误处理
- **Findings**:
  - Repository 使用临时文件、`fsync` 和 `os.replace`，写失败不会提交半成品。
  - 现有显示切换是先保存后应用；字号需求则明确要求失败时仍保持本次视觉变化。
- **Implications**:
  - 字号先应用到当前窗口，再尝试持久化。
  - 失败时显示错误并保留运行时字号，但不把内存中的“最后成功保存偏好”冒充为已保存值。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Notes |
|--------|-------------|-----------|---------------------|-------|
| viewport event filter | 主窗口过滤两个编辑 viewport 的滚轮事件 | 范围精确、无新控件层、易于验证 | 需明确 HTML/QSS 字体覆盖 | 采用 |
| 自定义 QTextBrowser/QTextEdit 子类 | 两种控件分别重写 `wheelEvent` | 局部封装 | 重复逻辑，新增无必要类型 | 不采用 |
| QApplication 全局过滤 | 在应用级拦截所有滚轮 | 单一入口 | 容易误伤段落列表、浏览页和设置 | 不采用 |

## Design Decisions

### Decision: 扩展既有显示偏好契约

- **Context**: 字号是用户级本地显示偏好，不是项目数据。
- **Alternatives Considered**:
  1. 直接使用 `QSettings` — 会形成第二套本地状态权威。
  2. 写入项目 JSON — 违反项目完整性和跨设备语义。
  3. 扩展 `DisplayPreferences` — 复用现有原子存储和 Controller 边界。
- **Selected Approach**: 在 frozen `DisplayPreferences` 增加 `editor_font_size`，范围为 10–28 逻辑像素，默认 15，步进为 1。
- **Rationale**: 与当前 15px 视觉基线兼容，并保持唯一状态权威。
- **Trade-offs**: 所有使用 `replace()` 的调用继续兼容；手工构造非法值会更早失败。
- **Follow-up**: 测试旧 workspace 缺少字段、非法字段以及边界值。

### Decision: UI 即时生效，持久化随后提交

- **Context**: 保存失败不能撤回用户刚刚看到的字号。
- **Alternatives Considered**:
  1. 保存成功后再应用 — 不满足失败时保留可见字号。
  2. 应用后保存 — 满足即时反馈与原子状态保护。
- **Selected Approach**: 主窗口先更新两个文本文档字体，再通过 Controller 保存新偏好；失败仅报告并保留当前窗口字体。
- **Rationale**: 将瞬时 UI 状态与最后成功持久化状态明确分开。
- **Trade-offs**: 保存失败后重启会恢复旧值，这是“不破坏此前有效状态”的预期结果。
- **Follow-up**: UI 测试注入保存失败，验证视觉字号不回滚。

## Risks & Mitigations

- source HTML 的固定字号重新出现会屏蔽缩放 — 用渲染测试和源码边界测试阻止内联 `font-size` 回归。
- event filter 安装到错误对象会拦截失败 — 使用真实 viewport 事件测试 source 与 target 两条路径。
- 新字段损坏导致其他显示偏好丢失 — 按字段解析字号并单独回退默认值。
- 连续滚轮越界 — contract 和 UI 两层 clamp，测试最小/最大边界。

## References

- `editor_contracts.py` — 现有跨层显示偏好契约。
- `workspace_state.py` — 本地原子 workspace 状态。
- `qt_editor_window.py` — source/target 控件、HTML 高亮和 Qt 样式入口。
- `.kiro/steering/structure.md` — Qt 只能经 `EditorController` 使用本地状态。

## Steering 同步检查

- **日期**：2026-07-27
- **触发事件**：`qt-editor-font-zoom` Design review
- **检查结果**：无需更新
- **理由**：该功能仅扩展现有本地显示偏好和 Qt 输入处理，不引入新层级、外部依赖、数据格式或产品定位变化。
