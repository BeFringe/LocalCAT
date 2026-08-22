# 技术栈

## 架构

依赖方向保持由前端向逻辑、解析/引擎和存储单向流动。

| 层 | 职责 | 当前关键文件 |
|----|------|-------------|
| Layer 1 Storage | legacy JSONL、canonical SQLite、mixed termbase CSV、资源/工作区状态与持久恢复 | `resource_repository.py`, `workspace_state.py`, `termbase_store.py`, `tm_sqlite_store.py`, `tm_activation_*.py` |
| Layer 2 Engine / Parser | TM retrieval/scoring、capability-gated text matcher、项目搜索、Trie 术语；中立 Parser contracts/source/registry 与格式 codec | `tm_retrieval.py`, `tm_similarity.py`, `text_matcher.py`, `project_search.py`, `glossary_engine.py`, `parser_contracts.py`, `parser_source.py`, `parser_registry.py`, `parser_*_codec.py` |
| Layer 3 Application / Logic | Parser composition/facade mapping、workspace immutable contracts/identity/explicit intake/reconciliation/carrier-neutral save+recovery/compatibility adapter、deterministic ProjectPackage ZIP v1、issued multi-document Controller session 与复合搜索投影、capability/runtime composition、TM adapter；Excel 无状态三态入口 | `parser_composition.py`, `editor_project.py`, `project_workspace_identity.py`, `project_workspace_contracts.py`, `editor_project_workspace_adapter.py`, `project_workspace_intake.py`, `project_workspace.py`, `project_save.py`, `project_package.py`, `project_search.py`, `workspace_state.py`, `resource_importer.py`, `tm_json_importer.py`, `capability_host.py`, `tm_application_composition.py`, `editor_tm_adapter.py`, `editor_controller.py`, `logic_controller.py` |
| Layer 4 Frontend | Excel、PySide6 主窗口/资源/术语交互与桌面启动入口 | `excel_adapter*.py`, `qt_editor_window.py`, `qt_settings_dialog.py`, `qt_termbase_dialog.py`, `qt_editor.py` |

关键约束：

- Engine、Parser、Repository 不得导入 PySide6 或 xlwings。
- Parser 与 Engine/Store 互不导入；Application 只经 `parser_contracts.py` 与 `parser_composition.py` 消费中立结果，只有 composition 可导入具体内建 codec。
- 多文档显式文件 intake 仅由 `project_workspace_intake.py` 通过中立 Parser surface 建立 rooted verified facts；`project_workspace.py` 只负责 carrier-neutral 聚合与 reconciliation，不打开 source、不枚举目录、不导入 Parser/codec、不拥有 writer 或 durable publication。
- `project_save.py` 仅协调 workspace snapshot、逐Document baseline、candidate/LKG、structured report 与 cold recovery；物理 ProjectPackage carrier、archive grammar、target path 与 durability primitive 仍属 C2C port，不得在 save service 内抢跑。
- `project_package.py` 实现 ADR-019 唯一 `localcat-project-package-zip-v1` carrier：仅接受 classic single-disk `ZIP_STORED`，拒绝 ZIP64/压缩/encryption/data descriptor/extra/comment/非闭集member；手工 export/import 复用 `project_save.py` 的 stage→validate→arm→publish→双readback→cleanup 协调，不得为sync/provider、ResourcePackage或codec-private语义owner。
- 多文档 Application 只通过 C2 owner 的 prepare/discard/commit capability 预构建并单点发布 Controller session；legacy `ProjectSearch*`/`RecentProject` 保持 exact，workspace 另用复合 `WorkspaceSearch*`/`RecentWorkspaceProject`，两种搜索共享同一 Core matcher pipeline。
- Qt 模块只调用 `EditorController` 与 frozen contracts，不得直接导入 repository、store、retrieval、matcher 或 capability owner。
- `CapabilityHost` 是 matcher/retrieval capability 发布权威；`TMRuntimeHost` 持有完整 resource snapshot，`EditorTMAdapter` 只将同一 operation 投影给 `EditorController`。
- `LogicController` 的三态与 legacy TM 优先规则保持不变；`EditorController` 单独持有 Qt 项目、搜索、TM/术语 issuance 与资源操作会话。

## 运行环境与依赖

- Python：当前在 3.14 上开发与验证。
- 核心与无头入口：标准库为主。
- Parser contracts/source/registry 与 JSON/TXT/PO/POT/TMX/normalized JSON codec 使用标准库；XLSX codec 仅在安全 preflight 后条件使用 openpyxl。
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
| JSON / TXT | `project_document` 单输入；JSON 支持 schema-v1 canonical write，TXT source-only/read-only |
| PO / POT | `project_document` singular profile；保留 opaque gettext metadata，plural 输入 fail closed |
| TMX | `translation_memory` Level 1；最大 100 MB；拒绝 DTD/ENTITY/外部实体；行内 XML 单元 warning+skip |
| normalized TM JSON | `translation_memory` 单文件数组根；单输入 codec 保序保重复，目录/LWW/JSONL 输出留在 CLI Application |
| CSV / XLSX | `termbase` 显式列选择；Qt 导入先消费 codec-owned 有界列 preview，并把完整 source identity 与可见列数绑定到正式导入；未提供选择的调用继续前两列兼容 preset，XLSX 只读 active worksheet且不聚合多 Sheet |
| workspace.json | 最近十个项目、稳定段落 ID/索引回退、显示/TM 偏好以及 ADR-014 批准的预处理规则/状态偏好；不写入翻译项目或执行会话 |

LocalCAT JSON canonical writer 由 codec 只生成确定性 bytes，`parser_source.py` 在 rooted target parent 内执行独占临时文件、file fsync、原子 replace 与 readback receipt；resource/store 与 normalized CLI 继续拥有各自事务。任何入口只有在 verified terminal 后才可提交，整体解析、consumer 或 commit 失败不得改变目标字节。托管资源删除先改名为 tombstone，再提交清单并在失败时回滚；外部资源只取消登记。

## 开发与测试标准

- 跨层契约使用 `@dataclass(frozen=True)` 和 tuple 集合。
- 新代码使用 `pathlib.Path`、现代类型语法和显式异常/结构化报告。
- 正式测试使用 stdlib `unittest`；Qt 使用 offscreen 与 QtTest；旧核心继续保留模块自检。
- Parser completion 使用合成可分发 golden、故障注入、mutation-sensitive completion matrix 与 AST/import closed-world guards；Gate D 100,000 条 TM 仍是 retrieval 性能资格，不是 Parser limit。
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
- Parser 与 Engine 按 ADR-015 保持互不导入；Application 兼容入口/adapter 映射中立 parsed records 与既有 Editor/TM/Termbase contract。SQLite 是 canonical TM 持久化基线，不归 Parser Foundation；TM ADR 决定 schema、迁移、snapshot 与 capability authority，benchmark 决定 Levenshtein/Dice 组合、候选策略、阈值与性能门。
- CSV/XLSX 术语列 preview 是 Parser reader 的显式 capability：CSV 复用首个逻辑 record，XLSX 复用 OPC preflight 与 active worksheet 首行；正式导入在同一新 sealed snapshot 上先复核完整身份和可见列数，再允许 verified stream 与 TermbaseStore 事务。Qt/Controller 不拥有 CSV/XLSX grammar，也不自动猜测语言列。
- TM candidate 模块解耦已按 ADR-017 落地：`tm_candidate_index.py` 只消费 `tm_candidate_store_contracts.py` 的中立 storage port，`tm_sqlite_candidate_projection.py` 独占 steady-state candidate SQL/row data plane；`SQLiteTMStore` / query view 继续独占 connection、transaction、generation、stable error mapping 与公开入口。schema、proof-query-v3、scorer/budget/order 未改，final roots 上 Gate C、fault/acceptance/release 及真实 100,000 条 FTS5/fallback Gate D 均已重签为 GO。
- Multi-Document 实现受 ADR-018/019 约束：ProjectPackage 是多文档 workspace 的 canonical persistence，ProjectOrigin 只描述 source/reconciliation/write-back，`codec_private_member` 保持 codec-owned opaque；ProjectPackage 与 ResourcePackage 不共享 authority。当前已落地 workspace v1 身份/origin、legacy 单 JSON promotion boundary、手动显式 JSON/TXT/PO/POT 聚合/reconciliation、carrier-neutral candidate/LKG/逐Document baseline/save+recovery，以及 ADR-019 严格 `localcat-project-package-zip-v1` 的 export/validate/preview/import/apply/receipt、冷重开和fault matrix。成功冷重开的package是durable workspace；未发布的显式文件intake仍为 `durable=False`。Qt 多文档章节 UI 与 Controller session仍属 Cluster 3/4，尚未实现。
