# 研究与设计决策

## Summary

- **Feature**：`language-resource-portability`
- **Discovery scope**：现有 canonical TM JSONL export/import、mixed legacy/v1 termbase Store、资源 Repository/Controller/Qt 入口，以及 Multi-Document C2C ProjectPackage 的安全包经验。
- **Current conclusion**：宜将“直接兼容文件”与“可传输 ResourcePackage”定义为同一 profile payload 的两个发布面，但不把单文件冒充为 package；package v1 一次只携带一个完整资源快照。

## Sources Consulted

- `.kiro/specs/language-resource-portability/brief.md`
- `.kiro/specs/tmx-context-interchange/brief.md`
- `.kiro/specs/multi-document-project-workspace/{requirements.md,design.md,research.md,tasks.md}` C2B/C2C
- `tm_migration.py`、`tm_snapshot_artifacts.py`、`tm_contracts.py`、`tm_sqlite_store.py`
- `termbase_store.py`、`editor_contracts.py`、`resource_importer.py`
- `resource_repository.py`、`editor_controller.py`、`qt_settings_dialog.py`
- `.kiro/specs/termbase-column-selection-import/{requirements.md,design.md,tasks.md}`

## Existing TM Export / Import Facts

`TMMigrationService.export_jsonl(store, destination)` 已经是一个成熟的 Core 出口：

- 在稳定 coordinator lease/read snapshot 中抓取 generation、head revision 与全部 canonical records；
- 以固定字段顺序输出 UTF-8 JSONL，保留 record id、source/target、speaker、context、file source、provenance、usage 和 origin 事实；
- 在同目录 exclusive temp 上流式写入/复读，为 JSONL 与相邻 SnapshotManifest 执行 replace/fsync/readback；
- 成功返回 `ExportReport`，与 `SnapshotReceipt`、destination digest、record count、generation/revision 闭合；失败返回带旧 destination preservation/recovery 证据的 `ExportFailure`；
- 不修改 canonical records/generation/head revision，也不清除 `SOURCE_DIVERGED`。

`TMMigrationService.import_snapshot()` / `rebuild_from_snapshot()` 已实现完整替换 active canonical generation 的 stage/seal/activation/rollback/recovery，而非将 JSONL 直接复制成 live SQLite。它目前绑定 configured JSONL/resource identity，因此 ResourcePackage apply 需要增加窄 Application/Core adapter，不能在 package 层绕过该状态机。

### Implication

TM ResourcePackage exporter 必须先让 `export_jsonl()` 向私有 staging destination 产生成功 `ExportReport`，然后将那份已验证 JSONL 原字节封装进 package。Package 层不读取 SQLite，不重写 JSONL grammar，也不将随机 operation/snapshot id 放进必须 deterministic 的 package manifest。底层 `ExportReport` 只进入操作 receipt 对账。

## Existing Termbase Facts

`TermbaseStore` 管理的 CSV 不是单一“两列术语表”：

- legacy 行为两列 `source,target`，匹配策略是 `legacy_case_sensitive_substring`；
- v1 行为六列 `localcat-term-v1,record_id,source,target,match_case,whole_word`；
- Store 在变更时以 UTF-8 BOM、LF 和 stdlib CSV writer 生成 deterministic bytes，并有 staged/recovery/replace/fsync/readback 事务；
- `list_records()` 可以完整复读 row kind、record id 与匹配 flags。

Parser 的 CSV/XLSX termbase reader 与显式列选择面用于将外部表格转换/合并为管理术语，它只读用户选定的 source/target 列，不是 v1 资源快照 reader。用它导入 ResourcePackage 会丢失 v1 record id 和匹配 flags。

### Implication

`localcat-termbase-csv-v1` 必须定义为 Store-owned managed snapshot profile：导出保留每行 legacy/v1 类型与精确语义，导入由 `TermbaseStore` 的新公开 snapshot validate/prepare-replace/commit 面消费。既有列选择导入继续是“外部表格 merge”，不与“快照 replace”合并。

## Existing Product Surface

- `ResourceRepository` 持久化本地 `ResourceConfig`，资源 path/kind 不可变；创建时分配本地 ID 和受管路径。
- `EditorController` 已经是资源列表、导入、TM lifecycle 与 term transaction 的 Application 边界。
- Qt 资源页当前只有 TMX/术语表导入以及管理/删除菜单，没有 TM JSONL、术语 CSV/v1 或 ResourcePackage 导出；现有 worker/busy/feedback 形状可作为非阻塞入口的 consumer seam。

## ProjectPackage Experience Reused as Lessons

Multi-Document C2C 已证明以下原则值得复用：

- 单一 immutable artifact 比 directory+pointer 更适合人工搬运和未来 provider bytes port；
- raw envelope preflight 必须先于宽松 archive library，且不 extract member；
- source 用 retained regular-file handle/sealed bytes 绑定，destination 用 parent device/inode + absent/existing digest/inode 绑定；
- preview 与 apply 消费同一私有验证计划，apply 前复证 source/destination；
- candidate 必须关闭后冷验证，publish/readback/cleanup 发生不确定时保留 LKG 和 recovery 事实。

但 ResourcePackage 不能从 ProjectPackage 继承以下语义：project/document/segment identity、source reconciliation、overlay、codec-private member、project session/revision、writer capability。本规格必须有独立 manifest、DTO、error code 和 apply 编排。

## Architecture Options

| Option | Strength | Risk | Decision |
|---|---|---|---|
| 直接上传 live SQLite/CSV | 无额外封装 | 携带 canonical authority、journal/stage 与不完整事务 | Reject |
| 只提供裸 JSONL/CSV | 人工备份直观 | 无统一 profile/preview/receipt，sync 难以 fail closed | Keep as direct export, not package |
| 目录式 ResourcePackage | 实现简单 | 手工复制/provider listing 可见部分成员 | Reject for v1 |
| deterministic stored ZIP | 单 artifact、可流式验证、适合后续 provider | 需要独立 raw ZIP 攻击面和限额 | Proposed v1 |
| 共同 Package base authority | 表面减少代码 | 项目身份与资源快照合并语义污染 | Reject |
| package import 默认 merge | 看似便利 | 不可逆、难冲突对账，不能用于 exact backup restore | Reject v1 |

## Frozen v1 Decisions

1. **Carrier**：独立 `localcat-resource-package-zip-v1`，strict `ZIP_STORED`、仅 `manifest.json` + 一个 profile payload；不导入/extends `project_package.py`。
2. **Cardinality**：v1 每包正好一个 TM 或术语资源快照；多资源 bundle 留待独立版本，不在 v1 预埋数组。
3. **Apply semantics**：只有 `create_new` / `replace_selected`；package 层没有 merge。现有 TMX 导入与 CSV/XLSX 术语列导入仍是独立 merge consumer。
4. **Manifest privacy/identity**：manifest 不存资源显示名、绝对路径、canonical store id 或操作 id；source-local resource id 只能进操作 receipt 并且不授权 destination。因此同 payload/profile 生成 deterministic package bytes。
5. **Durable receipt ledger**：Core/Application 在 repository-owned safe root 以 exact canonical JSON 原子保留 success/recovery receipt；该 ledger 是本地操作证据，不是资源 authority。Pending journal、LKG、stage 不进 package，也不成为 transferable metadata。
6. **First profile set + limits**：`localcat-resource-payload-set-v1` 仅含 `localcat-tm-jsonl-v1` 与 `localcat-termbase-csv-v1`；TMX 需由 `tmx-context-interchange` 批准新 profile-set 版本后接入。v1 artifact/payload 最大 512 MiB、manifest 1 MiB、正好 2 个 member、最多保留 256 个 safe issues；记录/字段 grammar 继续由对应 TM/Termbase owner 限制。

## Deferred / Out of Scope

- TMX payload/context/provenance/loss report 由 `tmx-context-interchange` 后续批准；本规格只为已批准的新 profile 保留 container adapter 接口。
- provider、凭据、加密、remote listing、调度、冲突合并属于 sync。
- ResourcePackage 不提供 TM context UI、Fuzzy 资格、TM Store 模块拆分或 canonical authority。
- ProjectPackage 保持项目 workspace 独立权威。
