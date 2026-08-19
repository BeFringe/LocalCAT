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
| **Feature 4.1: 单 JSON Qt MVP 增量** | Layer 3 + Layer 4 (Qt) | 🧭 Discovery | 分支 `ui-mvp` | speaker、搜索、预处理、术语管理 |
| **Feature 5: TM 存储与兼容搜索引擎** | Layer 1 + Layer 2 | 🧭 Discovery | 分支 `feature5` | SQLite、Levenshtein/Dice、Match Case/Whole Word |
| **Parser / Multi-document** | Shared + Layer 2B | 🧭 重新基线中 | 后置 | `.kiro/specs/parser-subsystem-extraction/` |

### Feature 1: 术语表引擎 (Glossary Engine)
- **架构层次**: Layer 2 (Core Engine)
- **实现内容**:
  - 基于 Trie 的高性能术语提取逻辑
  - 支持重叠匹配与长词优先策略
- **核心文件**: `glossary_engine.py`
- **Git 信息**: 分支 `feature/tm-engine`, 标签 `v0.1.0-feature1`
- **领域边界**: Feature 5 可提供共享文本匹配语义，但不接管 Trie、重叠命中、长词优先或术语 CRUD

### Feature 2: 翻译记忆库引擎 (TM Engine)
- **架构层次**: Layer 1 (Storage) + Layer 2 (Core Engine)
- **实现内容**:
  - 100% 精确匹配与追加式 JSONL 持久化存储
  - 提供遗留 PO 源单元读取与独立归一化 TM JSON CLI；尚未形成统一 Parser
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
  - 项目菜单、最近项目、退出当前项目，以及按稳定段落 ID 恢复上次翻译位置
  - 左栏紧凑等高/完整自动换行切换，以及双语全文浏览/校对页
  - 精确 TM 与术语并列建议、安全源文高亮、建议应用、术语插入与新增术语
  - 齿轮设置：资源新建/删除、Active/Lookup/Update、后台 TMX/CSV/XLSX 导入、内部路径解释与热重载
  - 对 `speaker "text"` MateCat/Ren'Py 记忆单元提供严格 100% 兼容匹配并安全解包译文
  - 使用 `LocalCAT-logo-silver.png` 的 Linux 用户应用菜单启动器，以及 `Ctrl+Shift+W` 退出项目、`Ctrl+Q` 退出应用
  - `Ctrl+O`、`Ctrl+S`、`Ctrl+Enter`、`Alt+Up`、`Alt+Down`、`Ctrl+,`
- **核心文件**: `qt_editor.py`, `qt_editor_window.py`, `qt_settings_dialog.py`, `editor_controller.py`, `workspace_state.py`
- **Git 信息**: 分支 `ui-mvp`

### Feature 4.1：单 JSON Qt MVP 增量 🧭

- 当前增量只承诺单 JSON 项目，不新增“打开 XLSX/RPY/TMX 项目”能力；现有 TXT 简单导入继续兼容但不是新增功能基线。
- 首批加入 raw speaker 显示、基础关键词搜索、文字预处理、术语 CRUD、silver logo 和紧凑“…”。
- Match Case / Whole Word 的 UI 入口属于本增量，但兼容 matcher 由 Feature 5 提供；合并前控件必须明确禁用并标注第二阶段。

### Feature 5：TM 存储与兼容搜索引擎 🧭

- 旧 `feature/logic-excel-adapter` README 和 `spec.md` 明确规划了 Levenshtein/Dice fuzzy；该升级目标仍保留，尚未实现。
- **主模块**：canonical TM record、SQLite TMStore、JSONL 迁移、Levenshtein/Dice scorer、exact → context → fuzzy 查询服务。
- **次模块**：Match Case / Whole Word 兼容文本 matcher、context/provenance、benchmark/阈值、旧 `TMEngine` façade 与 Controller adapter。
- SQLite 已确定为 TM 持久化基线；ADR 决定 schema/index/migration，真实 benchmark 决定 scorer 组合与候选策略。
- `LogicController` 的 `TM_HIT / TERMS_FOUND / NO_MATCH` 三态继续兼容；Qt 通过 `EditorController` 消费带分数的建议。
- 旧 Feature 5 中的 Docker、协作与部署已显式移出 Core Engine，不再作为 Feature 5 完成条件。

### Parser 与多文档项目：后置重新基线

- Parser 负责用途明确的 parsed records、`(purpose, format)` registry 和 codec 错误语义，不负责 TM 存储/评分或 Qt 搜索。
- 支持文件夹多 JSON、多 Sheet XLSX 和多 RPY 前，必须先引入 `Project → Document/Chapter → Segment`；当前扁平 `EditorProject` 不能可靠表达章节、相对来源和多文档保存。
- `CAT_Working_File.xlsx` 的 34 个 Sheet 证明 Sheet 名只能作显示名，稳定身份应来自 `File_ID`/source reference。
- TMX 保持语言资源 import/export；若未来编辑 TMX，应另做 TM Resource Editor，而不是把它注册成项目文档。

## 🚧 当前状态
- **最新标签**: Feature 3 (`v0.3.0-feature3`) - 无状态 Logic + Excel 适配器与性能基准
- **活动开发线**: `ui-mvp` 负责 Qt 单 JSON 编辑器增量，`feature5` 负责 TM 存储、检索和统一文本匹配能力
- **继承关系**: 两条开发线以已完成的 Qt 编辑器 MVP 为语义基线；共享变更只提交一次，并通过 merge/rebase 继承
- **规格状态**: Feature 5 已从原成功补丁链恢复；matcher capability 抢救性 Requirements 待重新批准，Design / Tasks / 实施门保持关闭
- **当前实施项**: `qt-editor-font-zoom` 已实现并通过完整回归与 offscreen 烟测；Qt JSON increment 继续等待 Feature 5 capability 契约闭合
- **合并方向**: Feature 5 从已验收 UI 基线演化，通过自身 gate 后合入 `ui-mvp`，再由 Qt 线接入 Controller adapter
- **明确后置**: 多文件/多 Sheet 项目、RPY/XLIFF codec、MyMemory context、MT、QA 与协作不伪装成当前已完成能力
- **连续性约束**: `.kiro/` 由 Git 跟踪；活动 worktree 不得放在 `/tmp` 或 tmpfs，阶段产物与已验证任务要及时形成可恢复提交

## 🛠 开发方法论

当前开发流程是**无常驻 Agent 状态的 Kiro 规格驱动开发**：

- 每次开发会话从仓库中的 `AGENTS.md`、`.kiro/steering/` 与 `.kiro/specs/` 恢复上下文；文件是持久事实来源。
- 需求、设计、任务、实现和验证按阶段留痕；Qt MVP 的规格位于 `.kiro/specs/qt-editor-mvp/`，Parser 重新基线调研位于 `.kiro/specs/parser-subsystem-extraction/`。
- `plugins/modular-cat-architect/` 是早期配合旧 `main` README 的遗留材料，已过时，不再作为现行方法论或架构裁决来源。
- 运行时也区分两种状态语义：旧 `LogicController` 继续保持无状态三态转发；Qt 的 `EditorController` 在进程内持有当前编辑会话，`WorkspaceStateRepository` 只把最近项目、段落断点和显示偏好原子保存在本地。

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

# Linux：安装到用户应用菜单，之后可像普通桌面应用一样启动
python qt_editor.py --install-desktop-launcher

# macOS：先退出正在运行的 LocalCAT，再原子安装并签名
# ~/Applications/LocalCAT.app
python qt_editor.py --install-macos-app
```

仓库根目录的 `LocalCAT-launcher` 是供安装器封装的原生可执行模板，不能直接双击当作应用。macOS 安装命令会先在隐藏候选目录中构建并整包 ad-hoc 签名，完成真实冷启动验证后才原子发布 `~/Applications/LocalCAT.app`；已有 LocalCAT 运行时会拒绝替换，原安装保持不变。若曾手动拖入 `/Applications`，请先退出该实例，再选择保留手动副本或使用上述用户级安装位置，避免同时存在两个同 bundle id 的副本。

缺少 PySide6 时，启动器会输出上述安装命令而不会显示未处理 traceback。macOS 轻量应用保留安装时的 Python 与当前 checkout 绝对路径；移动/删除这些路径后需重新执行安装命令。应用数据默认写入操作系统的本地应用数据目录，也可用 `--data-dir PATH` 覆盖。进入项目后可从顶栏“项目”菜单打开或切换最近项目、退出当前项目；再次打开同一项目会恢复最后段落。

## 📁 MVP 支持范围

| 类型 | 支持 |
|------|------|
| 编辑项目 | 单文件 `.json` 为当前增量基线；已有按非空行分段的 `.txt` 继续兼容；保存为版本化 UTF-8 JSON |
| 翻译记忆库 | 本地 JSONL；设置中导入 TMX Level 1，明确指定源/目标 locale |
| 术语表 | 本地 UTF-8-SIG 两列 CSV；设置中导入 CSV/XLSX 前两列 |
| 匹配 | 100% 精确 TM；严格 `speaker "text"` 兼容；Trie 术语命中与最长非重叠高亮 |

这里的 TMX/CSV/XLSX 是**语言资源导入格式**，不是“打开项目”格式。安全限制：单个导入文件最大 100 MB；含 DTD/ENTITY 的 TMX 被拒绝；含 XML 行内元素的 TU 会跳过并计入反馈；失败导入不替换原资源。当前不含模糊匹配、机器翻译、QA、账户、云端或多人协作。

### 外部 Rpy 辅助工具与 TMX

同级目录中的 `RpySeriesExtract` 与 `RpyExtended` 是外部辅助 Python 项目，不是 LocalCAT 的运行依赖。两者的 `renpy_local_tool.py` 核心状态机基本一致：Series 有 Git/README 可追踪性和更多样本，Extended 有修复后产物。两者均未发现许可证，且现有脚本不是流式/无损实现；未来 RPY codec 只把它们作为行为参照，使用合成 fixture 独立重写，不直接复制代码或游戏数据。

- MateCat 导出的无 DTD TMX（如 `OWNattempt.tmx`）可由设置导入；以 `en-US → zh-CN` 导入 165 条、跳过 67 个缺少语言对的 TU、覆盖 30 个重复源文。在 `po/卷一_引.json` 的 2942 段中产生 112 个普通正文精确命中。
- 对大型 MateCat/Ren'Py TMX，导入后列表显示 `~/.local/share/LocalCAT/resources/*.jsonl` 属于正常行为：TMX 已转换并合并为 LocalCAT 的内部运行时格式。项目把 speaker 与正文分开、而 TMX 保存为 `speaker "text"` 时，编辑器会做严格同 speaker 精确兼容并只应用解包后的译文；这不是模糊匹配。
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
