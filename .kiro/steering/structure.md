# 项目结构

## 组织原则

项目继续采用根目录平铺模块，但通过明确的 Layer 与自动化边界测试约束依赖：

```text
Layer 4 Qt / Excel / desktop bootstrap
        ↓
Layer 3 EditorController / adapters / capability-runtime hosts
        ↓
Layer 2 Parser / matcher / retrieval / engines
        ↓
Layer 1 resource / termbase / canonical TM storage
```

`LogicController` 是 Excel 兼容所需的无状态三态入口；`EditorController` 是 Qt 专用的会话协调入口。两者职责不同，不互相调用。

## 关键目录

```text
/
├── editor_contracts.py          # Qt 编辑器 frozen 跨层契约
├── editor_project.py            # JSON/TXT 单输入项目 Application facade
├── project_workspace_identity.py # 多文档稳定 ID、portable ref 与 source digest 中立叶
├── project_workspace_contracts.py # Project/Document/Segment/Origin immutable contracts
├── editor_project_workspace_adapter.py # legacy 单 JSON ↔ workspace 唯一兼容映射层
├── project_workspace_intake.py # 显式多文件 rooted intake 与 Parser verified facts 映射
├── project_workspace.py       # carrier-neutral workspace 聚合、progress 与 reconciliation
├── project_save.py            # carrier-neutral candidate/LKG、baseline、save report 与 cold recovery
├── project_package.py         # deterministic ZIP v1 ProjectPackage、严格验证与手工导入导出事务
├── parser_contracts.py          # stdlib-only 中立合同、capability 与 limit profile
├── parser_source.py             # rooted source/snapshot/terminal 与 canonical 原子写
├── parser_registry.py           # purpose-aware 不可变 registry，不导入具体 codec
├── parser_composition.py        # 唯一内建注册点与 Application surface
├── parser_json_support.py       # 有界 JSON lexical/depth/full-input preflight
├── parser_xlsx_support.py       # ZIP/OPC XML/DTD/ENTITY 安全 preflight
├── parser_localcat_codec.py     # LocalCAT JSON/TXT reader 与 JSON canonical serializer
├── parser_gettext_codec.py      # PO/POT singular project-document codec
├── parser_tmx_codec.py          # TMX Level 1 资源 codec
├── parser_tm_json_codec.py      # normalized TM JSON 单输入资源 codec
├── parser_termbase_codec.py     # CSV/XLSX 显式列选择资源 codec
├── resource_repository.py       # 资源清单和受控本地文件
├── workspace_state.py           # legacy/复合最近项目断点、显示/TM 与设备本地预处理偏好
├── resource_importer.py         # TMX/CSV/XLSX Application policy 与事务导入
├── renpy_tm_compat.py           # 严格 speaker 对话封装查询与目标解包
├── project_search.py            # legacy/workspace 双入口、单一 matcher pipeline 搜索服务
├── editor_controller.py         # Qt 单文件与多文档 issued session/搜索/保存协调
├── editor_tm_adapter.py         # runtime + capability 同次 operation 投影
├── capability_host.py           # Matcher/Gate C/Gate D 发布与 host lifecycle
├── tm_application_composition.py # legacy/canonical resource resolver/runtime host
├── qt_editor.py                 # stdlib composition/bootstrap 与 desktop install CLI
├── qt_editor_window.py          # Layer 4 主编辑器、多文档章节/保存反馈、TM 与项目搜索
├── qt_settings_dialog.py        # Layer 4 语言资源设置
├── qt_termbase_dialog.py        # Layer 4 集中式术语管理
├── qt_control_styles.py         # Layer 4 共享 popup/menu 视觉合同
├── qt_tm_threshold.py           # 双入口 fuzzy threshold 共享控件
├── logic_controller.py          # 旧 Excel 无状态三态入口
├── excel_adapter*.py            # Excel Layer 4
├── glossary_engine.py
├── termbase_store.py             # mixed legacy/v1 termbase transaction owner
├── text_matcher.py               # 唯一 BASIC/TEXT_V1 Unicode matcher
├── capability_gated_text_matcher.py # matcher capability execution boundary
├── tm_contracts.py              # canonical TM frozen contracts
├── tm_candidate_store_contracts.py # candidate DTO/error/port 中立叶合同
├── tm_candidate_index.py        # candidate budget/stage/proof algorithm
├── tm_engine.py                 # legacy/canonical cold-open compatibility owner
├── tm_sqlite_store.py           # per-resource store/coordinator authority
├── tm_sqlite_candidate_projection.py # SQLite candidate SQL/row data plane
├── tm_retrieval.py              # exact/context/fuzzy query pipeline
├── tm_retrieval_capability.py   # retrieval capability evaluator/publisher
├── tm_retrieval_validation.py   # current-source Gate C validation
├── tm_benchmark_gate.py          # owner-issued Gate D benchmark/publication
├── tm_activation_journal.py     # activation durable records/protocol
├── tm_activation_recovery.py    # activation completion/rollback
├── tm_schema_upgrade.py         # schema copy data plane + upgrade artifacts
├── tm_snapshot_artifacts.py     # snapshot artifact namespace/proof/handoff primitives
├── tm_migration.py              # migration/import/export/upgrade orchestration
├── macos_app_launcher.py        # user-local .app atomic builder/validator
├── macos/LocalCATLauncher.c     # native execv bootstrap source
├── LocalCAT-launcher             # universal arm64/x86_64 Mach-O asset
├── tools/generate_multi_document_current_source_evidence.py # Multi-Document final-roots evidence owner
├── multi_document_current_source_evidence.json # 17-root canonical current-source evidence
├── tests/                       # unittest、Qt offscreen、QtTest、架构守卫
├── .kiro/specs/                 # 需求/设计/任务与验证事实
└── .kiro/steering/              # 当前项目级产品、技术与结构约束
```

`po/` 与 `workloads/` 保留翻译数据和基准夹具；根目录 `tm.jsonl`、`terms.csv` 作为首次启动默认资源注册。

## 导入规则

- `qt_editor_window.py`、`qt_settings_dialog.py`、`qt_termbase_dialog.py` 只可导入 `EditorController` 与 frozen contracts，不可导入 repository/store/retrieval/matcher/capability owner。
- `editor_controller.py` 可协调项目编解码、资源仓储、application adapters 和既有引擎，不导入 PySide6。
- `editor_tm_adapter.py` 只消费 host-issued runtime/capability snapshot 和 ports，不成为新的存储、scorer 或 capability authority。
- `capability_host.py` 协调 Matcher/Gate C/Gate D owner 与 application handoff；不把 evidence/receipt 暴露给 Controller 或 Qt。
- `workspace_state.py` 只保存 Qt 无关的本地工作区状态；ADR-014 的 preprocessing member 仅包含规则与状态偏好，不保存项目正文、preview/session/revision 或 undo。Qt 前端不得直接访问它。
- `logic_controller.py` 不导入 Qt/xlwings，保持无历史状态的三态接口。
- `parser_contracts.py` 只依赖标准库；Parser Foundation 与各 codec 不导入 Engine、Store、Controller、Qt、workspace 或 sync/provider implementation。
- `parser_registry.py` 不导入具体 codec；只有 `parser_composition.py` 显式注册内建 codec，并向 Application 提供选择、打开、验证、materialize、stream 与 canonical write surface。
- `editor_project.py`、`resource_importer.py`、`logic_controller.py` 与 `tm_json_importer.py` 只负责既有类型映射、batch policy 和事务，不保留第二份 JSON/TXT/PO/POT/TMX/CSV/XLSX 语法。
- `project_workspace_identity.py` 与 `project_workspace_contracts.py` 是 Qt/Engine/Store/provider/chunk/resource 无关的中立叶；`editor_project_workspace_adapter.py` 是 legacy 单 JSON 与 workspace 的唯一兼容映射层，只通过 `parser_contracts.py` / `parser_composition.py` 取得 verified terminal，不拥有 JSON grammar 或 writer。
- `project_workspace_intake.py` 仅对用户显式选中的 JSON/TXT/PO/POT 保留单次 rooted batch binding，通过 Parser 中立 surface 取得 verified facts；不枚举目录、不导入具体 codec/parser_source、不授予 source writer。`project_workspace.py` 只消费已验证 immutable facts，拥有扁平投影、progress 与 reconciliation，不导入 Parser/intake/Qt/TM/Store/provider/chunk/resource。
- `project_save.py` 只消费 workspace 的 canonical digest/service/contracts，拥有 carrier-neutral save candidate、真实 LKG/逐Document baseline、结构化报告与 cold-recovery 阶段协调；不导入 Parser/codec、物理 archive/carrier、Qt、TM/Store、provider、chunk 或 ResourcePackage。当前显式选择的 JSON/TXT/PO/POT 仍只保存 package overlay，不执行 source write-back。
- `project_package.py` 是 ADR-019 批准的唯一 ProjectPackage v1 carrier owner：它拥有严格 `ZIP_STORED` envelope/manifest/member grammar、bounded stream、artifact/content digest、手工 export/validate/preview/import/apply/receipt 与物理 recovery port；不导入具体 codec/registry、Qt、TM/Store、provider、chunk 或 ResourcePackage，不 extract member，不解释 `codec_private_member`。
- `editor_controller.py` 的多文档入口只消费 C1–C2 frozen service/receipt，以 session/generation/revision 签发 Project/Document/Segment view；source reconcile 与 active package import 使用 owner-issued prepare/discard/commit capability，候选投影完成后才单点切换 session。`project_search.py` 让 legacy 与 workspace request 共用一个 matcher pipeline；workspace 使用独立复合 hit/report，不扩宽既有单 JSON DTO。Qt 不直接取得 workspace、manifest、member、persistence binding 或 recovery authority。
- openpyxl 只由 `parser_termbase_codec.py` 在 XLSX preflight 通过后条件导入，并固定 read-only/data-only、关闭 links/VBA；`resource_importer.py` 不拥有 active-sheet 或列选择语法。
- `renpy_tm_compat.py` 是 Qt 无关纯函数兼容桥，不解析 `.rpy`、不依赖 Engine/Repository；它只服务 legacy exact lane，canonical TM 不经该桥。
- `tm_schema_upgrade.py` 只消费 frozen contracts、activation 共用错误与 owner 注入的窄 plan/callback；不反向导入 `tm_sqlite_store.py` 或 `tm_migration.py`，不拥有 coordinator 状态。
- `tm_snapshot_artifacts.py` 只拥有 snapshot/export deterministic artifact family、no-follow parent dirfd、strict file identity/digest proof、exclusive temp/recovery copy、replace/cleanup 原语与 durable handoff 值编解码；不反向导入 `tm_sqlite_store.py`、`tm_migration.py` 或 `tm_snapshot_recovery.py`，不拥有 ledger/binding/transaction 或 receipt reconciliation 状态。
- `tm_candidate_index.py` 只消费 `tm_candidate_store_contracts.py` 的中立 port/DTO；`tm_sqlite_candidate_projection.py` 是 steady-state candidate recall/proof/write SQL 与 row decode 的唯一数据面 owner，仅使用调用方持有的 connection/transaction。
- `tm_sqlite_store.py` / query view 继续独占 connection policy、lease、BEGIN/COMMIT/ROLLBACK、generation/head/count、stable error mapping 与 publication；projection 不打开、提交或发布 canonical authority。
- 核心 Engine 不向上导入 Controller 或 Frontend。

该边界由 `tests/test_parser_architecture_harness.py`、`tests/test_parser_wave4_architecture.py`、Qt AST 守卫和 Excel 适配器契约测试持续验证。

## 命名与代码风格

- 文件、函数：`snake_case`；类：`PascalCase`；常量：`UPPER_SNAKE_CASE`。
- 新代码使用 `from __future__ import annotations`、`pathlib.Path` 和 PEP 604 union。
- 跨层数据为 frozen dataclass；集合使用 tuple，操作结果使用结构化 report。
- Qt 控件设置稳定 `objectName`，便于 QtTest 与可访问性验证。
- CLI `main()` 返回整数并由 `raise SystemExit(main())` 结束。

## 测试布局

- `tests/test_editor_*`：纯逻辑、项目和资源协调。
- `tests/test_multi_document_cluster*`：多文档 workspace 的 current-source characterization、分 Cluster 架构边界、identity/origin/package/session 与最终验收。
- `tests/test_project_search*`、`tests/test_qt_project_search*`：单 JSON 搜索 contracts/service/Controller/Qt 与 current-source acceptance。
- `tests/test_feature5_ui_*`：真实 canonical activation/retrieval、mixed merge、failure 与 apply/write 跨层验收。
- `tests/test_capability_host*`、`tests/test_tm_retrieval*`：capability publication、in-flight generation 与 Core query 语义。
- `tests/test_tm_candidate_store_contracts.py`、`tests/test_tm_sqlite_candidate_projection.py`、`tests/test_tm_store_candidate_projection_*`：candidate 叶 port、SQL 唯一 owner、transaction/fault 与兼容 seam。
- `tests/test_workspace_state.py`：最近项目、段落断点、显示/TM 与预处理偏好的兼容读取、原子持久化和失败保留。
- `tests/test_resource_*`：清单、托管/外部删除、TMX/CSV/XLSX、原子失败语义。
- `tests/test_renpy_tm_compat.py`：安全 speaker token、引号转义与拒绝猜测性解包。
- `tests/test_qt_*`：offscreen 组件、后台导入、项目菜单、密度/浏览模式、窗口工作流和真实鼠标/键盘旅程。
- `tests/test_excel_adapter_contract.py`：Excel 三态和层级边界。
- `tests/test_parser_*`：contracts/source/registry/composition、八个用途/格式组合、golden/fault/security/facade 与跨格式 completion 矩阵。
- `tests/test_macos_app_launcher.py`：Finder/LaunchServices identity、cwd-independent bootstrap、atomic replacement 与失效路径。
- `tests/test_tm_schema_upgrade_module_boundaries.py`：schema-upgrade 依赖方向、owner 权威与 late-bound 兼容接缝。
- `tests/test_tm_snapshot_artifacts_module_boundaries.py`：snapshot artifact 依赖方向、owner 权威、late-bound fault seam 与移动等价性。
- 五个旧脚本自检仍是发布前回归矩阵的一部分。

## 开发上下文

当前方法是无常驻 Agent 状态的 Kiro 规格驱动开发。持久上下文位于 `AGENTS.md`、`.kiro/steering/` 和 `.kiro/specs/`；早期 `plugins/modular-cat-architect/` 仅为历史材料，不得覆盖当前 steering 或实现事实。Parser 已按 `parser-subsystem-extraction` 的批准 Requirements/Design/Tasks 就地重新基线；更早的同目录遗留草案仍只作历史留档。
