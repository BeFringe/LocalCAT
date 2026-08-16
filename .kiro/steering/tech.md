# 技术栈

## 架构

依赖方向保持由前端向逻辑、解析/引擎和存储单向流动。

| 层 | 职责 | 当前关键文件 |
|----|------|-------------|
| Layer 1 Storage | JSONL/CSV、版本化资源/工作区状态、原子替换 | `resource_repository.py`, `workspace_state.py`, `tm_engine.py` |
| Layer 2 Engine / Parser | 精确 TM、Trie 术语、项目与语言资源解析、严格 Ren'Py TM 兼容 | `glossary_engine.py`, `editor_project.py`, `resource_importer.py`, `renpy_tm_compat.py` |
| Layer 3 Logic | Excel 无状态三态入口；Qt 有状态会话协调 | `logic_controller.py`, `editor_controller.py` |
| Layer 4 Frontend | Excel 与 PySide6 桌面交互 | `excel_adapter*.py`, `qt_editor_window.py`, `qt_settings_dialog.py` |

关键约束：

- Engine、Parser、Repository 不得导入 PySide6 或 xlwings。
- Qt 模块只调用 `EditorController`，不得直接导入 `ResourceRepository`、`TMEngine`、`GlossaryEngine` 或 `LogicController`。
- `LogicController` 的三态与 TM 优先规则保持不变；`EditorController` 同时返回 TM 与术语建议并持有当前编辑会话。

## 运行环境与依赖

- Python：当前在 3.14 上开发与验证。
- 核心与无头入口：标准库为主。
- Qt MVP：`PySide6==6.11.1`。
- XLSX：`openpyxl>=3.1,<4`。
- 交互式 Excel：xlwings，可选且只属于 Excel Layer 4。
- Qt 依赖入口：`requirements-ui.txt`。

```bash
python -m pip install --user -r requirements-ui.txt
python qt_editor.py --sample
python qt_editor.py --install-desktop-launcher
```

`qt_editor.py` 顶层只导入标准库，完成参数解析后才加载 PySide6 和窗口模块。Linux 安装流程通过 `xdg-icon-resource` 把 `LocalCAT-logo-silver.png` 注册为用户主题中已由 `hicolor/index.theme` 声明的 `512x512/apps/localcat` 图标，`.desktop` 使用稳定图标名并刷新桌面数据库；不要安装到未声明的 `1024x1024/apps`，GTK 菜单查找会忽略该目录。缺 Qt 时输出安装提示并返回非零，缺 openpyxl 时只有 XLSX 导入失败。

## 数据与安全规则

| 格式 | 用途与规则 |
|------|------------|
| JSONL | TM 内部运行时存储；精确匹配、最后写入胜出；TMX 导入后列表显示该路径属于正常行为 |
| UTF-8-SIG CSV | 本地两列术语表 |
| JSON / TXT | 编辑项目输入；项目保存为版本化 JSON |
| TMX | Level 1 导入；最大 100 MB；拒绝 DTD/ENTITY；行内 XML 单元跳过 |
| XLSX | 术语导入前两列；依赖 openpyxl |
| workspace.json | 最近十个项目、稳定段落 ID/索引回退、列表密度和工作区模式；不写入翻译项目 |

项目保存、资源清单与导入均使用同目录临时文件加 `os.replace`。整体解析失败不得改变目标字节。托管资源删除先改名为 tombstone，再提交清单并在失败时回滚；外部资源只取消登记。

## 开发与测试标准

- 跨层契约使用 `@dataclass(frozen=True)` 和 tuple 集合。
- 新代码使用 `pathlib.Path`、现代类型语法和显式异常/结构化报告。
- 正式测试使用 stdlib `unittest`；Qt 使用 offscreen 与 QtTest；旧核心继续保留模块自检。
- `.kiro/specs/` 保存需求、设计和任务；`.kiro/steering/` 保存当前项目级事实。

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen python qt_editor.py --smoke-test
python glossary_engine.py
python tm_engine.py
python logic_controller.py
python stress_runner.py
python translation_runner.py
```

## 关键技术决策

- Qt 会话状态属于 `EditorController`，不塞回旧无状态 `LogicController`。
- 最近项目、最后段落与显示偏好由 Qt 无关的 `WorkspaceStateRepository` 原子保存；前端仍只通过 `EditorController` 访问。
- 资源状态由 `ResourceRepository` 原子持久化，设置对话框不直接写文件。
- 普通 source exact 优先；仅当 speaker token 安全且源/目标均为同 speaker 封装时，兼容桥才解包 Ren'Py/MateCat 记录。
- Active + Lookup 决定查询集合；Active + Update 决定确认写回集合。
- 导入后先构建完整新引擎集合，成功后一次替换；失败保留上一组可用实例。
- 浏览/校对页与三栏编辑器共享同一个 `EditorProject` 会话，只读表格不复制或覆盖未保存译文。
- 当前只提供精确 TM，不伪装模糊匹配或云端能力。
- Parser 与 Engine 保持相互独立；SQLite 已确定为 Feature 5 TM 持久化基线，不归 Parser Foundation。storage/index ADR 决定 schema、迁移与索引，benchmark 决定 Levenshtein/Dice 组合、候选策略、阈值与性能门。
