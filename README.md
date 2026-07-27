# LocalCAT

LocalCAT 是一款轻量级、模块化、本地优先的计算机辅助翻译（CAT）工具。

## 🌟 核心愿景
针对商业 CAT 工具日益严重的商业化限制（额度限制、隐私风险、免费用户负优化），LocalCAT 旨在提供一个完全受控的本地化翻译环境。

- **100% 本地化**：翻译记忆库（TM）与术语表（Glossary）均存储在本地，不强制联网。
- **模块化设计**：核心逻辑与 UI 完全解耦，支持从简单的 Excel 协作过渡到专业的 QT 界面。
- **高性能**：采用前缀树（Trie）等高效算法处理超大规模语料。

## 🏗 系统架构
LocalCAT 采用四层依赖方向，核心引擎与界面保持隔离：

1. **Layer 1 - Storage（持久层）**：JSONL TM、UTF-8-SIG CSV 术语表、版本化资源清单与原子文件替换。
2. **Layer 2 - Core Engine / Parser（核心引擎与解析）**：精确 TM、Trie 术语匹配、JSON/TXT 项目和安全 TMX/CSV/XLSX 导入。
3. **Layer 3 - Logic（交互逻辑层）**：`LogicController` 保留 Excel 所需的无状态三态接口；`EditorController` 专门维护 Qt 编辑会话并协调本地资源。
4. **Layer 4 - Frontend（表示层）**：xlwings/openpyxl Excel Adapter 与 PySide6 Qt Desktop。

Qt 前端只能调用 `EditorController`，不得直接调用资源仓储、TM 或术语引擎。详细 MVP 设计见 [Qt 编辑器规格](.kiro/specs/qt-editor-mvp/design.md)。

## 🚀 开发里程碑 (Development Roadmap)

| Feature | 架构层次 | 状态 | Git 标签 | 核心文件 |
|---------|---------|------|---------|---------|
| **Feature 1: 术语表引擎** | Layer 2 | ✅ 已完成 | v0.1.0-feature1 | `glossary_engine.py` |
| **Feature 2: 翻译记忆库引擎** | Layer 1 + Layer 2 | ✅ 已完成 | v0.2.0-feature2 | `tm_engine.py`, `tm.jsonl` |
| **Feature 3: 逻辑层与 Excel 适配器** | Layer 3 + Layer 4 (Excel) | ✅ 已完成 | v0.3.0-feature3 | `logic_controller.py`, `excel_adapter.py` |
| **Feature 4: Qt 专业编辑器 MVP** | Layer 3 + Layer 4 (Qt) | ✅ 已完成 | 分支 `ui-mvp` | `editor_controller.py`, `qt_editor_window.py`, `qt_settings_dialog.py` |
| **Feature 5: 模糊匹配与自动化** | Layer 2 增强 | 🔮 计划中 | - | - |

### Feature 1: 术语表引擎 (Glossary Engine)
- **架构层次**: Layer 2 (Core Engine)
- **实现内容**:
  - 基于 Trie 的高性能术语提取逻辑
  - 支持重叠匹配与长词优先策略
- **核心文件**: `glossary_engine.py`
- **Git 信息**: 分支 `feature/tm-engine`, 标签 `v0.1.0-feature1`

### Feature 2: 翻译记忆库引擎 (TM Engine)
- **架构层次**: Layer 1 (Storage) + Layer 2 (Core Engine)
- **实现内容**:
  - 100% 精确匹配与追加式 JSONL 持久化存储
  - 支持 PO/JSON 文件导入
  - 系统集成压力测试验证
- **核心文件**: `tm_engine.py`, `tm.jsonl`, `stress_runner.py`
- **Git 信息**: 分支 `feature/tm-engine`, 标签 `v0.2.0-feature2`

### Feature 3: 逻辑层与 Excel 适配器 (Logic Layer & Excel Adapter)
- **架构层次**: Layer 3 (Logic UI) + Layer 4 (Frontend - Excel)
  - Layer 3: `logic_controller.py` - 无状态逻辑转发
  - Layer 4: `excel_adapter.py`, `excel_adapter_openpyxl.py` - Excel 前端适配
- **实现内容**:
  - 无状态逻辑转发层（TM 优先策略）
  - Excel 手动触发工作流（xlwings 交互式 + openpyxl 文件模式）
  - 性能基准测试（5/50/200/800 行，冷/热启动，分段计时）
- **核心文件**: `logic_controller.py`, `excel_adapter.py`, `excel_adapter_openpyxl.py`
- **Git 信息**: 分支 `feature/logic-excel-adapter`, 标签 `v0.3.0-feature3`

### Feature 4: Qt 专业编辑器 MVP (Qt Desktop Editor) ✅
- **架构层次**: Layer 3 (`EditorController`) + Layer 4 (PySide6)
- **已实现内容**:
  - MateCat 风格的本地三栏编辑器：段落列表、源文/译文、Translation Matches 与 Termbase
  - JSON/TXT 项目打开、版本化 JSON 原子保存、未保存保护、确认进度与未确认导航
  - 精确 TM 与术语并列建议、安全源文高亮、建议应用、术语插入与新增术语
  - 齿轮设置：资源新建、Active/Lookup/Update、后台 TMX/CSV/XLSX 导入与热重载
  - `Ctrl+O`、`Ctrl+S`、`Ctrl+Enter`、`Alt+Up`、`Alt+Down`、`Ctrl+,`
- **核心文件**: `qt_editor.py`, `qt_editor_window.py`, `qt_settings_dialog.py`, `editor_controller.py`
- **Git 信息**: 分支 `ui-mvp`

### Feature 5: 模糊匹配与自动化 (Fuzzy Matching & Automation) 🔮
- **架构层次**: Layer 2 (Core Engine) 增强
- **计划内容**:
  - Levenshtein/Dice 系数模糊匹配
  - Docker 容器化部署
  - 协作翻译功能

## 🚧 当前状态
- **最新稳定版**: Feature 2 (v0.2.0-feature2) - 核心引擎完成
- **开发分支**: Feature 3 (feature/logic-excel-adapter) - 逻辑层与 Excel 适配器已完成，已推送
- **最新版本**: Feature 3 (v0.3.0-feature3) - 逻辑层与 Excel 适配器 + 性能基准测试
- **Qt MVP 分支**: `ui-mvp` - Feature 4 已实现并通过本地回归，尚未推送
- **下一步**: 在真实个人项目上试用 MVP；模糊匹配、MT、QA 和协作仍不在当前范围

## 🛠 开发方法论

当前开发流程是**无常驻 Agent 状态的 Kiro 规格驱动开发**：

- 每次开发会话从仓库中的 `AGENTS.md`、`.kiro/steering/` 与 `.kiro/specs/` 恢复上下文；文件是持久事实来源。
- 需求、设计、任务、实现和验证按阶段留痕；Qt MVP 的规格位于 `.kiro/specs/qt-editor-mvp/`。
- `plugins/modular-cat-architect/` 是早期配合旧 `main` README 的遗留材料，已过时，不再作为现行方法论或架构裁决来源。
- 运行时也区分两种状态语义：旧 `LogicController` 继续保持无状态三态转发；Qt 的 `EditorController` 仅在进程内持有当前编辑会话，项目与资源仍落在本地文件。

## 🖥️ 安装与启动 Qt MVP

已在 Python 3.14 与 PySide6 6.11.1 上验证。

```bash
python -m pip install --user -r requirements-ui.txt

# 打开空状态
python qt_editor.py

# 直接载入示例
python qt_editor.py --sample

# 打开项目
python qt_editor.py --project path/to/project.json
```

缺少 PySide6 时，启动器会输出上述安装命令而不会显示未处理 traceback。应用数据默认写入操作系统的本地应用数据目录，也可用 `--data-dir PATH` 覆盖。

## 📁 MVP 支持范围

| 类型 | 支持 |
|------|------|
| 编辑项目 | `.json`、按非空行分段的 `.txt`；保存为版本化 UTF-8 JSON |
| 翻译记忆库 | 本地 JSONL；设置中导入 TMX Level 1，明确指定源/目标 locale |
| 术语表 | 本地 UTF-8-SIG 两列 CSV；设置中导入 CSV/XLSX 前两列 |
| 匹配 | 100% 精确 TM；Trie 术语命中与最长非重叠高亮 |

安全限制：单个导入文件最大 100 MB；含 DTD/ENTITY 的 TMX 被拒绝；含 XML 行内元素的 TU 会跳过并计入反馈；失败导入不替换原资源。当前不含模糊匹配、机器翻译、QA、账户、云端或多人协作。

### 外部 Rpy 辅助工具与 TMX

同级目录中的 `RpySeriesExtract` 与 `RpyExtended` 是外部辅助 Python 项目，不是 LocalCAT 的运行依赖。两者都带 TMX 与 `tmx_extract_tool.py`；从现有代码看，`RpyExtended` 版本功能更完整（locale 规范化、回退、批处理和过滤），但仓库不假定二者的正式版本关系，也不复制其实现。

- MateCat 导出的无 DTD TMX（如 `OWNattempt.tmx`）可由设置导入；本地兼容烟测导入 165 条、跳过 67 个缺少语言对的 TU。
- `chinese__english.tmx` 含外部 DOCTYPE，按 LocalCAT 的安全边界会被拒绝。若需要使用，应先在受信环境中生成不含 DTD/ENTITY 的 TMX，而不是降低编辑器的 XML 安全策略。

## ✅ 验证

```bash
# 新增 unittest 与 Qt offscreen GUI 回归
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v

# Qt 启动闭环
QT_QPA_PLATFORM=offscreen python qt_editor.py --smoke-test

# 既有五个入口回归
python glossary_engine.py
python tm_engine.py
python logic_controller.py
python stress_runner.py
python translation_runner.py
```

Excel 无头文件模式与交互适配器边界由 `tests/test_excel_adapter_contract.py` 独立验证；Qt 层禁止直接导入仓储或引擎的规则由 AST 回归测试守护。
