# 技术栈

## 架构

依赖方向保持由前端向逻辑、解析/引擎和存储单向流动。

| 层 | 职责 | 当前关键文件 |
|----|------|-------------|
| Layer 1 Storage | legacy JSONL、canonical SQLite、mixed termbase CSV、资源/工作区状态与持久恢复 | `resource_repository.py`, `workspace_state.py`, `termbase_store.py`, `tm_sqlite_store.py`, `tm_activation_*.py` |
| Layer 2 Engine / Parser | TM retrieval/scoring、capability-gated text matcher、项目搜索、Trie 术语、项目/资源解析 | `tm_retrieval.py`, `tm_similarity.py`, `text_matcher.py`, `project_search.py`, `glossary_engine.py`, `editor_project.py` |
| Layer 3 Application / Logic | capability/runtime composition、TM adapter、Qt 有状态会话；Excel 无状态三态入口 | `capability_host.py`, `tm_application_composition.py`, `editor_tm_adapter.py`, `editor_controller.py`, `logic_controller.py` |
| Layer 4 Frontend | Excel、PySide6 主窗口/资源/术语交互与桌面启动入口 | `excel_adapter*.py`, `qt_editor_window.py`, `qt_settings_dialog.py`, `qt_termbase_dialog.py`, `qt_editor.py` |

关键约束：

- Engine、Parser、Repository 不得导入 PySide6 或 xlwings。
- Qt 模块只调用 `EditorController` 与 frozen contracts，不得直接导入 repository、store、retrieval、matcher 或 capability owner。
- `CapabilityHost` 是 matcher/retrieval capability 发布权威；`TMRuntimeHost` 持有完整 resource snapshot，`EditorTMAdapter` 只将同一 operation 投影给 `EditorController`。
- `LogicController` 的三态与 legacy TM 优先规则保持不变；`EditorController` 单独持有 Qt 项目、搜索、TM/术语 issuance 与资源操作会话。

## 运行环境与依赖

- Python：当前在 3.14 上开发与验证。
- 核心与无头入口：标准库为主。
- Qt 前端：`PySide6==6.11.1`。
- XLSX：`openpyxl>=3.1,<4`。
- 交互式 Excel：xlwings，可选且只属于 Excel Layer 4。
- Qt 依赖入口：`requirements-ui.txt`。

```bash
python -m pip install --user -r requirements-ui.txt
python qt_editor.py --sample
python qt_editor.py --install-desktop-launcher
python qt_editor.py --install-macos-app
```

`qt_editor.py` 顶层只导入标准库，完成参数解析后才加载 PySide6 和窗口模块。Linux 安装流程使用用户级 `.desktop` 与主题图标；macOS 安装流程在 sibling candidate 中验证 universal native launcher、plist/icon 和 LaunchServices 冷启动，再原子替换 user-local `LocalCAT.app`。该 lightweight bundle 绑定安装时的绝对 Python/bootstrap，不复制 Python/PySide；路径变化后必须重新安装。缺 Qt 时输出安装提示并返回非零，缺 openpyxl 时只有 XLSX 导入失败。

## 数据与安全规则

| 格式 | 用途与规则 |
|------|------------|
| JSONL | legacy TM 的 exact/source-LWW 兼容存储，也是首次 canonical 激活的可核对 source snapshot |
| SQLite + sidecars/journal | 每资源 canonical TM；版本化记录、多译文、索引、generation、snapshot binding 与可崩溃恢复发布 |
| UTF-8-SIG CSV | mixed termbase；legacy 两列行原样保留，v1 行携带稳定 id 与 Match Case / Whole Word |
| JSON / TXT | 编辑项目输入；项目保存为版本化 JSON |
| TMX | Level 1 导入；最大 100 MB；拒绝 DTD/ENTITY；行内 XML 单元跳过 |
| XLSX | 术语导入前两列；依赖 openpyxl |
| workspace.json | 最近十个项目、稳定段落 ID/索引回退、显示/TM 偏好以及 ADR-014 批准的预处理规则/状态偏好；不写入翻译项目或执行会话 |

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
- 最近项目、最后段落、显示/TM 偏好与 ADR-014 的设备本地预处理偏好由 Qt 无关的 `WorkspaceStateRepository` 原子保存；前端仍只通过 `EditorController` 访问。
- 资源状态由 `ResourceRepository` 原子持久化，设置对话框不直接写文件。
- legacy 普通 source exact 优先；仅当 speaker token 安全且源/目标均为同 speaker 封装时，兼容桥才解包 Ren'Py/MateCat 记录。
- Active + Lookup 决定查询集合；Active + Update 决定确认写回集合。
- TM/termbase 候选图均先完整构建和验证，成功后一次替换；失败保留上一组可用实例或明确 fail closed。
- 浏览/校对页与三栏编辑器共享同一个 `EditorProject` 会话，只读表格不复制或覆盖未保存译文。
- canonical 查询固定 EXACT → CONTEXT → FUZZY；Gate D 按 ADR-013 由 Core 复证设备本地资格，兼容键命中可跨进程恢复，缺失/失配只允许显式重验。FUZZY 仍只在正式 capability 开放且候选分数达到 device-local 阈值时出现，从不自动应用。
- 已发布 canonical 的跨重启平台文件身份恢复按 ADR-016；普通打开、内容证明、generation 与 Fuzzy 资格边界保持不变。
- 项目搜索与版本化术语共用 capability-gated `TextMatcher`的 Unicode/Whole Word 语义；Qt 不复制 matcher 实现。
- Parser 与 Engine 按 ADR-015 保持互不导入；Application façade/adapter 映射中立 parsed records 与既有 Editor/TM/Termbase contract。SQLite 是 canonical TM 持久化基线，不归 Parser Foundation；TM ADR 决定 schema、迁移、snapshot 与 capability authority，benchmark 决定 Levenshtein/Dice 组合、候选策略、阈值与性能门。
