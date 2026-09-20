# 产品概览

LocalCAT 是面向个人译者的轻量、模块化、本地优先 CAT 工具。翻译项目、记忆库、术语表与配置均保存在本机，不要求账号、云端或遥测。

## 当前交付形态

Windows 11 source 已完成 CPython 3.14 x64 专用 venv、源码和轻量 `LocalCAT Source` launcher 的产品闭环，包括项目保存/重开、TM 激活/重启恢复、TMX 导入与 FTS5 查询。用户管理 Python、依赖和源码位置；launcher 只提供开始菜单入口和环境失效诊断。

Windows 首次 frozen 是 0.5.2 的当前交付目标：普通 PyInstaller onedir/windowed 自带 Python/Qt，并在同一候选上完成 Qt、Project、TM、TMX、FTS5 用户旅程、数据保护及 Core Gate C/D 验证。打包烟测只是实施步骤，不拆成单独的可行性发行。W3 不阻塞 source 可用或继续演进。Linux 应用菜单与 macOS 轻量 `.app` 同样依赖外部运行时；不能把轻量 launcher 宣称为自包含发行。长期取舍集中在 [交付与验证边界](delivery-boundaries.md)，安装步骤见根目录 README。

## 产品演进

Feature 1/2 建立 Trie 术语与 JSONL 精确 TM；Feature 3 形成 Excel 无状态工作流；Feature 4/4.1 引入 Qt 编辑会话、搜索、预处理和术语管理。Feature 5 将 canonical TM 演进为 SQLite、多译文、EXACT/CONTEXT/FUZZY，并完成 Qt 集成；0.5.1 交付统一 Parser 与多文档基线。既有 Excel 和 JSONL 兼容入口继续保留。

0.5.2 聚焦 Windows 首次自包含交付；0.5.3 规划 `rpy-project-codec` 与后续 `cross-device-sync-plugin`。macOS frozen 尚未立项，可能作为 0.5.4，不能据此列为已批准任务。已发布标签与各阶段能力见根目录 README。

## 当前产品能力

1. **Qt 专业编辑器**：PySide6 三栏编辑器提供段落导航、源文/译文编辑、确认进度、未保存保护、快捷键和本地项目保存；左栏可切换紧凑等高与完整换行。
2. **项目搜索**：可搜 source、target 与 raw speaker，支持 Match Case、Whole Word 和未填写/草稿/已翻译状态筛选；多文档可选当前章节、全部章节或已激活分工范围，命中使用稳定段身份导航，不覆盖未保存译文。
3. **翻译记忆库**：legacy JSONL 继续提供 source-LWW exact 兼容；激活后的 canonical SQLite 提供多译文、EXACT/CONTEXT/FUZZY、确定性 top-10、可调 60%～100% 阈值、安全 TMX 导入和确认写回。
4. **术语表**：保留 legacy 两列 CSV 和 Trie 重叠/最长优先语义；版本化记录支持每条 Match Case / Whole Word、集中式 CRUD、本地事务恢复与 CSV/XLSX 导入。
5. **语言资源设置**：Active、Lookup、Update、新建/删除/导入、legacy 与 canonical 状态、显式激活/重建、术语管理与无需重启的运行时换代。
6. **Excel 兼容入口**：旧 `LogicController` 保持 `TM_HIT / TERMS_FOUND / NO_MATCH` 无状态三态契约，继续服务 xlwings 与 openpyxl 适配器。
7. **不可变跨层契约**：编辑项目、段落、搜索命中、TM/术语建议、资源状态和操作报告使用 frozen dataclass。
8. **可恢复桌面工作区**：项目菜单、最近项目、稳定段落断点与本地偏好，并提供 Linux、macOS 与 Windows source 桌面入口。
9. **浏览/校对模式**：只读双语全文按段自动换行，双击任一行回到同一段专业编辑区。
10. **多文档与分工**：显式选择多个 JSON/TXT/PO/POT 输入，按文档导航，以 ProjectPackage 保存和恢复；协作分工按稳定段落集合拆分、合并、分配和查看进度，不包含在线协作或自动同步。
11. **资源交换**：TM JSONL、术语 CSV/v1 支持独立 ResourcePackage 导出、预览和导入应用；TMX 支持资源、项目和选定分工范围导出，其 ResourcePackage profile 仍为 export-only。Fuzzy 资格不随资源跨设备搬运。

## 目标用户与核心流程

- 个人译者打开单文件项目、显式选择多个文档或打开 ProjectPackage，搜索并导航到稳定段，在当前段同时查看 canonical/legacy TM 与术语建议，显式应用后再编辑确认。
- 用户从齿轮设置创建、启停、删除或导入语言资源；legacy TM 可显式激活 canonical，术语表可进入集中管理，完成后当前段建议立即更新。
- 用户在紧凑导航、完整换行和双语浏览之间切换；退出或重开项目时恢复上次段落和显示方式。
- 既有 Excel 工作流继续通过无状态逻辑接口获得三态结果，不因 Qt MVP 改变。

## 价值主张

- **本地与私密**：不发送项目或语言资源到网络。
- **可控与可恢复**：资源清单、项目保存、导入、托管资源删除和工作区断点均采用显式错误与原子/回滚语义。
- **前后端隔离**：Qt 只调用 `EditorController`，不直接依赖资源仓储或引擎。
- **兼容演进**：Qt 使用有状态编辑会话；旧 Excel 入口继续保持无状态，不混淆两种职责。

## 当前非目标

机器翻译、QA/校对通过层、账户、云端同步、公共/共享 TM、实时多人协作和复杂带标签 TMX 不属于当前产品。RPY/XLIFF 项目 codec、多 Sheet workbook 与目录自动发现仍为后续范围；现有分工和多文档能力不等于这些后续能力已完成。
