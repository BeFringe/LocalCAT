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
├── editor_project.py            # JSON/TXT 项目与原子保存
├── resource_repository.py       # 资源清单和受控本地文件
├── workspace_state.py           # 最近项目、段落断点与显示偏好
├── resource_importer.py         # TMX/CSV/XLSX 安全原子导入
├── renpy_tm_compat.py           # 严格 speaker 对话封装查询与目标解包
├── project_search.py            # capability-gated 单项目搜索服务
├── editor_controller.py         # Qt 项目/搜索/TM/术语/资源会话
├── editor_tm_adapter.py         # runtime + capability 同次 operation 投影
├── capability_host.py           # Matcher/Gate C/Gate D 发布与 host lifecycle
├── tm_application_composition.py # legacy/canonical resource resolver/runtime host
├── qt_editor.py                 # stdlib composition/bootstrap 与 desktop install CLI
├── qt_editor_window.py          # Layer 4 主编辑器、TM 与项目搜索
├── qt_settings_dialog.py        # Layer 4 语言资源设置
├── qt_termbase_dialog.py        # Layer 4 集中式术语管理
├── qt_tm_threshold.py           # 双入口 fuzzy threshold 共享控件
├── logic_controller.py          # 旧 Excel 无状态三态入口
├── excel_adapter*.py            # Excel Layer 4
├── glossary_engine.py
├── termbase_store.py             # mixed legacy/v1 termbase transaction owner
├── text_matcher.py               # 唯一 BASIC/TEXT_V1 Unicode matcher
├── capability_gated_text_matcher.py # matcher capability execution boundary
├── tm_contracts.py              # canonical TM frozen contracts
├── tm_engine.py                 # legacy/canonical cold-open compatibility owner
├── tm_sqlite_store.py           # per-resource store/coordinator authority
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
- `workspace_state.py` 只保存 Qt 无关的本地工作区状态；Qt 前端不得直接访问它。
- `logic_controller.py` 不导入 Qt/xlwings，保持无历史状态的三态接口。
- `resource_importer.py` 不导入 PySide6；openpyxl 仅在 XLSX 路径中条件导入。
- `renpy_tm_compat.py` 是 Qt 无关纯函数兼容桥，不解析 `.rpy`、不依赖 Engine/Repository；它只服务 legacy exact lane，canonical TM 不经该桥。
- `tm_schema_upgrade.py` 只消费 frozen contracts、activation 共用错误与 owner 注入的窄 plan/callback；不反向导入 `tm_sqlite_store.py` 或 `tm_migration.py`，不拥有 coordinator 状态。
- `tm_snapshot_artifacts.py` 只拥有 snapshot/export deterministic artifact family、no-follow parent dirfd、strict file identity/digest proof、exclusive temp/recovery copy、replace/cleanup 原语与 durable handoff 值编解码；不反向导入 `tm_sqlite_store.py`、`tm_migration.py` 或 `tm_snapshot_recovery.py`，不拥有 ledger/binding/transaction 或 receipt reconciliation 状态。
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
- `tests/test_project_search*`、`tests/test_qt_project_search*`：单 JSON 搜索 contracts/service/Controller/Qt 与 current-source acceptance。
- `tests/test_feature5_ui_*`：真实 canonical activation/retrieval、mixed merge、failure 与 apply/write 跨层验收。
- `tests/test_capability_host*`、`tests/test_tm_retrieval*`：capability publication、in-flight generation 与 Core query 语义。
- `tests/test_workspace_state.py`：最近项目、段落断点和显示偏好持久化。
- `tests/test_resource_*`：清单、托管/外部删除、TMX/CSV/XLSX、原子失败语义。
- `tests/test_renpy_tm_compat.py`：安全 speaker token、引号转义与拒绝猜测性解包。
- `tests/test_qt_*`：offscreen 组件、后台导入、项目菜单、密度/浏览模式、窗口工作流和真实鼠标/键盘旅程。
- `tests/test_excel_adapter_contract.py`：Excel 三态和层级边界。
- `tests/test_macos_app_launcher.py`：Finder/LaunchServices identity、cwd-independent bootstrap、atomic replacement 与失效路径。
- `tests/test_tm_schema_upgrade_module_boundaries.py`：schema-upgrade 依赖方向、owner 权威与 late-bound 兼容接缝。
- `tests/test_tm_snapshot_artifacts_module_boundaries.py`：snapshot artifact 依赖方向、owner 权威、late-bound fault seam 与移动等价性。
- 五个旧脚本自检仍是发布前回归矩阵的一部分。

## 开发上下文

当前方法是无常驻 Agent 状态的 Kiro 规格驱动开发。持久上下文位于 `AGENTS.md`、`.kiro/steering/` 和 `.kiro/specs/`；早期 `plugins/modular-cat-architect/` 仅为历史材料，不得覆盖当前 steering 或实现事实。Parser 遗留草案也不得直接实施，需按同目录 `research.md`/`rebaseline-plan.md` 重新走审批。
