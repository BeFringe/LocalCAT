# LocalCAT

LocalCAT 是面向个人译者的轻量、模块化、本地优先 CAT 工具。项目、翻译记忆库（TM）、术语表与配置保存在本机，不要求账号、云端或遥测。

## Windows 快速开始

适用于 Windows 11 x64，解压即可运行，无需安装 Python 或 Qt。

1. 将 Windows ZIP 完整解压到本机硬盘上的一个文件夹。
2. 打开其中的 `LocalCAT/LocalCAT.exe`。请保留同文件夹里的其他文件。
3. 在首页打开项目，选择段落后即可编辑译文。已有翻译记忆库或术语表可在「语言资源」设置中导入。

**首次使用模糊匹配**：在「语言资源」设置中点击「重新验证 Fuzzy」。首次验证可能需要数十分钟，期间可以继续编辑和保存；下次启动会复用仍然有效的验证结果。如果完成后提示检索较慢，仍可使用模糊匹配。

项目保存在你选择的位置，配置和语言资源默认保存在 `%LOCALAPPDATA%\LocalCAT`。更新时先退出程序，再解压新版；保留自己的项目和资源。

已知问题：个别情况下，重开项目不能回到上次浏览的段落。[验证结果与已知限制](.kiro/specs/windows-platform-enablement/cross-spec-amendments.md#时间准入修订候选-681a)中保留了详细说明。

## 产品演进与开发里程碑

LocalCAT 从 **Trie 术语 + JSONL 精确 TM → Excel 无状态工作流 → Qt 编辑会话 → SQLite canonical TM 与兼容检索 → Parser、多文档与资源交换** 逐步演进。早期 Excel 入口继续兼容，Qt 已成为主要桌面界面；SQLite 承载激活后的 TM，JSONL 保留兼容与交换用途。

| 里程碑 | 产品与架构增量 | 状态 / Git 标签 |
| --- | --- | --- |
| Feature 1 · 0.1.0 | Trie 术语引擎，重叠命中与长词优先 | 已完成 · `v0.1.0-feature1` |
| Feature 2 · 0.2.0 | JSONL 追加式 TM 与 100% 精确匹配 | 已完成 · `v0.2.0-feature2` |
| Feature 3 · 0.3.0 | 无状态 Logic 三态接口，xlwings / openpyxl Excel 工作流 | 已完成 · `v0.3.0-feature3` |
| Feature 4 · 0.4.0 | Qt 三栏编辑器、项目保存/恢复、资源设置与建议交互 | 已完成 · `v0.4.0-feature4` |
| Feature 4.1 · 0.4.1 | raw speaker、项目搜索、target 预处理与术语管理 | 已完成 · `v0.4.1-feature4.1` |
| Feature 5 · 0.5.0 | canonical SQLite、多译文、JSONL 迁移、EXACT/CONTEXT/FUZZY、统一文本匹配；接入 Qt 建议、阈值与资源生命周期 | Core 与 UI 集成已完成 · `v0.5.0-feature5`、`v0.5-feature5-integration` |
| 0.5.1 · Parser / Multi-document | 统一用途感知 Parser、多文档身份与 ProjectPackage，承接分工、资源交换及 TMX 互操作 | 已完成 · `v0.5.1-parser+multidoc` |
| **0.5.2 · Windows 首次 frozen** | Windows source 与轻量 launcher，以及普通 PyInstaller onedir/windowed 自包含交付，用户无需安装 Python/Qt | 已发布 · [`v0.5.2-windows-frozen`](https://github.com/BeFringe/LocalCAT/releases/tag/v0.5.2-windows-frozen) |
| 0.5.3 · RPY codec 与同步 | 先由 `rpy-project-codec` 接入 Ren'Py translation script，再由 `cross-device-sync-plugin` 消费项目包与资源包 | 规划中 |
| 可能的 0.5.4 · macOS frozen | macOS 自包含发行；现有轻量 `.app` 仍依赖外部 Python 和源码 | 尚未立项，版本未定 |

已发布标签见 [GitHub Tags](https://github.com/BeFringe/LocalCAT/tags)。表中的规划版本不是发布承诺。

### 架构随产品演进

- **Storage**：legacy JSONL/CSV、canonical SQLite、项目与资源包，以及各自的保存/恢复状态。
- **Engine / Parser**：TM 检索与评分、Trie 术语、统一文本匹配和用途明确的格式 codec。
- **Application / Logic**：`LogicController` 保留 Excel 无状态三态；`EditorController` 协调 Qt 编辑会话、多文档和资源能力。
- **Frontend**：PySide6 Qt 与既有 Excel 适配器。界面消费应用层，不另建存储或匹配权威。

## 当前可用范围

- **Qt 编辑器**：三栏编辑、确认进度、未保存保护、最近项目、段落断点、快捷键、字号调整和双语浏览/校对。
- **项目与搜索**：单文件 JSON/TXT、显式多文档 JSON/TXT/PO/POT、章节导航与 ProjectPackage 保存/恢复；搜索 source、target、raw speaker，支持 Match Case、Whole Word 和状态筛选。
- **翻译记忆库**：legacy JSONL 精确兼容；激活后的 canonical SQLite 提供多译文、EXACT/CONTEXT/FUZZY、确定性 top-10、60%～100% 阈值和确认写回。Fuzzy 需本机资格验证，不自动应用建议。
- **术语与资源**：Trie 术语建议、术语 CRUD、CSV/XLSX 显式列选择导入、TMX 导入；TM JSONL 与术语 CSV/v1 可通过 ResourcePackage 导出、预览及导入应用。
- **分工与交换**：按稳定段落划分/分配本地协作任务；TMX 可从托管资源、项目或选定分工导出。TMX ResourcePackage 当前仅支持导出与验证，不支持导入应用。
- **Excel 兼容入口**：保留 xlwings/openpyxl 的既有无状态工作流；Qt 不依赖 Microsoft Excel 或 xlwings。

RPY/XLIFF 项目 codec、多 Sheet workbook、目录自动发现、机器翻译、QA、账户、云端同步和实时多人协作仍属后续范围。TMX/CSV/XLSX 的语言资源支持不等于相同格式可作为编辑项目打开。

## 从源码运行（Windows）

Windows 11 的 **CPython 3.14 x64 + 专用 venv + source + 轻量 launcher** 已完成用户旅程验收：真实 Qt 窗口、项目保存/冷重开、TM 激活/重启恢复、TMX 导入、FTS5 查询，以及头像与无匹配回退。

从源码运行需要自行安装 Python 和依赖，并保留源码目录。使用上方 Windows ZIP 的用户可跳过本节。

### 安装与启动

准备已安装的 CPython 3.14 x64，以及本地 NTFS 上的完整 LocalCAT 源码目录。在该目录打开 PowerShell，执行：

```powershell
# 使用已安装的 CPython 3.14 创建专用环境
py -3.14 -m venv .venv

# 不需要激活脚本，直接使用环境中的 Python
.\.venv\Scripts\python.exe -m pip install -r requirements-ui.txt
.\.venv\Scripts\python.exe -m pip check

# 可先打开示例；退出窗口后安装开始菜单入口
.\.venv\Scripts\python.exe qt_editor.py --sample
.\.venv\Scripts\python.exe qt_editor.py --install-windows-launcher
```

若系统没有 `py` 命令，把第一条命令的 `py -3.14` 换为已安装 CPython 3.14 x64 的绝对 `python.exe` 路径（PowerShell 中使用 `& '路径'`）。已有符合条件的专用 venv 可以直接复用，无需重新创建。

之后从当前用户的开始菜单打开 **LocalCAT Source**。入口绑定安装时 venv 的绝对 `pythonw.exe` 和源码目录，以 isolated mode 启动 guardian，再启动真正的 Qt 进程。安装只写入 guardian、图标和快捷方式，不复制 Python、PySide6、Qt 或业务源码。

也可以从源码目录直接启动：

```powershell
.\.venv\Scripts\python.exe qt_editor.py
.\.venv\Scripts\python.exe qt_editor.py --project 'D:\Translation\project.json'
```

### 更新与故障处理

- **更新源码**：退出 LocalCAT，更新到要使用的已验收源码版本，在同一 venv 重新执行依赖安装和 `pip check`，再执行 `--install-windows-launcher` 更新入口。不要在运行中替换应用文件。
- **移动源码或 venv**：从新位置重建或选择有效的专用 venv，再安装 launcher。现成 venv 和快捷方式绑定本机路径，不应作为可移植安装包分发。
- **启动失败**：先用上述 `python.exe qt_editor.py` 命令查看终端诊断。guardian 会对源码、依赖或 child 异常显示 `LOCALCAT.WINDOWS.SOURCE.*` 诊断码，并在本地应用数据目录记录；若 venv 已被删除，Windows 会先报告快捷方式目标失效。
- **数据位置**：默认配置与托管资源在 `%LOCALAPPDATA%\LocalCAT`，项目文件保存在用户选定位置。修复 launcher 无需删除用户数据；诊断或迁移时应先保留项目和资源。

当前 source 面向已验证的本地 Windows 文件系统能力；UNC/network share、FAT/exFAT 和未经验证的文件系统不在交付范围。

## Linux / macOS 启动

使用 Python 3.14 和专用环境安装 `requirements-ui.txt`（PySide6 6.11.1、openpyxl），然后运行：

```bash
python -m pip install -r requirements-ui.txt
python qt_editor.py --sample

# Linux 用户应用菜单
python qt_editor.py --install-desktop-launcher

# macOS：先退出 LocalCAT，再安装用户级轻量应用
python qt_editor.py --install-macos-app
```

macOS 安装器在候选目录构建并签名，冷启动验证后原子发布 `~/Applications/LocalCAT.app`。根目录的 `LocalCAT-launcher` 是安装模板，不能直接双击作为应用。若保留手动安装的 `/Applications` 副本，应避免同时使用两个相同 bundle id 的实例。

macOS 普通终端启动会在创建 Qt 前经 LaunchServices 打开已签名入口，并绑定本次 checkout；终端等待应用退出。`--smoke-test` 与安装命令保持直接 Python 路径。轻量应用仍依赖外部 Python 和源码，路径改变后需重装；它与 Windows source launcher 一样，不是自包含发行。

## 数据与能力边界

- 项目、TM 和资源各自拥有保存、发布与恢复语义；失败不能静默替换原数据。Windows 使用自己的句柄、锁和 ACL/MIC 端口，POSIX 使用本机原语。
- TMX 拒绝 DTD/ENTITY/外部实体；含不支持 XML 行内元素的单元会跳过并反馈。大型导入仍受格式限额约束。
- canonical Fuzzy 启动时恢复兼容的本机资格；缺失或失配时从设置中手动验证，期间可以继续编辑和保存。完整验证中仅时间指标超限时，Fuzzy 仍可使用并显示性能提示，性能报告继续保留失败；正确性、内存等必要条件仍限制准入。JSONL/ResourcePackage 搬运数据，不搬运资格。
- 普通 frozen 保留必要数据保护、真实 Core Gate 和包内业务验收，按 ADR-028 收窄旧 W3 的完整启动来源证明；0.5.2 当前候选已完成发行裁决。

## 开发与验证

0.5.2 使用普通 PyInstaller onedir/windowed 打包，当前支持本地 NTFS。候选 `681a898d51c7` 已在同一产物上完成 Qt、Project、TM、TMX、FTS5 用户旅程、必要的数据保护及 Core Gate C/D 验证，最终发行裁决为 **VERIFIED**；性能结果及复用范围见[验收记录](.kiro/specs/windows-platform-enablement/cross-spec-amendments.md#时间准入修订候选-681a)。构建和资源/SQLite 烟测只是其中的步骤。

依赖方向为 Qt / Excel → Application / Logic → Engine / Parser → Storage。Qt 经 `EditorController` 消费不可变契约；Parser 与 Engine/Store 互不导入。旧 `LogicController` 保持 Excel 所需的无状态三态接口。

Windows PowerShell 启动烟测（不替代完整验收）：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe qt_editor.py --smoke-test
Remove-Item Env:QT_QPA_PLATFORM
```

开发时按改动范围运行 `python -m unittest ...`；完整 Qt 回归使用 `QT_QPA_PLATFORM=offscreen`。发行验证另需真实可见窗口、保存/恢复和各 owner 的 Gate，不能仅凭烟测或构建退出码通过。

- [Windows source launcher 验收](windows_user_managed_launcher_evidence.json)与[完整用户旅程](qt_editor_windows_source_evidence.json)
- [Windows 任务与分线状态](.kiro/specs/windows-platform-enablement/tasks.md)
- [产品概览](.kiro/steering/product.md)、[路线图](.kiro/steering/roadmap.md)与[交付边界清单](.kiro/steering/delivery-boundaries.md)

项目使用仓库中的 `AGENTS.md`、`.kiro/steering/` 与各 owning Spec 保存上下文。早期 `plugins/modular-cat-architect/` 仅为历史材料；产品演进与当前完成状态以上述里程碑和正式规格为准。
