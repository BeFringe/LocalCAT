# 项目结构

## 组织原则

项目继续采用根目录平铺模块，但通过明确的 Layer 与自动化边界测试约束依赖：

```text
Layer 4 Qt / Excel
        ↓
Layer 3 EditorController / LogicController
        ↓
Layer 2 Parser / Engine
        ↓
Layer 1 Storage
```

`LogicController` 是 Excel 兼容所需的无状态三态入口；`EditorController` 是 Qt 专用的会话协调入口。两者职责不同，不互相调用。

## 关键目录

```text
/
├── editor_contracts.py          # Qt 编辑器 frozen 跨层契约
├── editor_project.py            # JSON/TXT 项目与原子保存
├── resource_repository.py       # 资源清单和受控本地文件
├── workspace_state.py           # 最近项目、段落断点与显示偏好
├── resource_importer.py         # TMX/CSV/XLSX 安全原子导入
├── editor_controller.py         # Qt 会话、项目断点、查询、写回、热重载
├── qt_editor.py                 # 标准库 bootstrap
├── qt_editor_window.py          # Layer 4 主编辑器
├── qt_settings_dialog.py        # Layer 4 语言资源设置
├── logic_controller.py          # 旧 Excel 无状态三态入口
├── excel_adapter*.py            # Excel Layer 4
├── glossary_engine.py
├── tm_engine.py
├── tests/                       # unittest、Qt offscreen、QtTest、架构守卫
├── .kiro/specs/                 # 需求/设计/任务与验证事实
└── .kiro/steering/              # 当前项目级产品、技术与结构约束
```

`po/` 与 `workloads/` 保留翻译数据和基准夹具；根目录 `tm.jsonl`、`terms.csv` 作为首次启动默认资源注册。

## 导入规则

- `qt_editor_window.py`、`qt_settings_dialog.py` 只可导入 `EditorController` 与 frozen contracts，不可导入仓储或引擎。
- `editor_controller.py` 可协调项目编解码、资源仓储、导入器和现有引擎，不导入 PySide6。
- `workspace_state.py` 只保存 Qt 无关的本地工作区状态；Qt 前端不得直接访问它。
- `logic_controller.py` 不导入 Qt/xlwings，保持无历史状态的三态接口。
- `resource_importer.py` 不导入 PySide6；openpyxl 仅在 XLSX 路径中条件导入。
- 核心 Engine 不向上导入 Controller 或 Frontend。

该边界由 `tests/test_qt_user_journey.py` 的 AST 守卫和 Excel 适配器契约测试持续验证。

## 命名与代码风格

- 文件、函数：`snake_case`；类：`PascalCase`；常量：`UPPER_SNAKE_CASE`。
- 新代码使用 `from __future__ import annotations`、`pathlib.Path` 和 PEP 604 union。
- 跨层数据为 frozen dataclass；集合使用 tuple，操作结果使用结构化 report。
- Qt 控件设置稳定 `objectName`，便于 QtTest 与可访问性验证。
- CLI `main()` 返回整数并由 `raise SystemExit(main())` 结束。

## 测试布局

- `tests/test_editor_*`：纯逻辑、项目和资源协调。
- `tests/test_workspace_state.py`：最近项目、段落断点和显示偏好持久化。
- `tests/test_resource_*`：清单、TMX/CSV/XLSX、原子失败语义。
- `tests/test_qt_*`：offscreen 组件、后台导入、项目菜单、密度/浏览模式、窗口工作流和真实鼠标/键盘旅程。
- `tests/test_excel_adapter_contract.py`：Excel 三态和层级边界。
- 五个旧脚本自检仍是发布前回归矩阵的一部分。

## 开发上下文

当前方法是无常驻 Agent 状态的 Kiro 规格驱动开发。持久上下文位于 `AGENTS.md`、`.kiro/steering/` 和 `.kiro/specs/`；早期 `plugins/modular-cat-architect/` 仅为历史材料，不得覆盖当前 steering 或实现事实。
