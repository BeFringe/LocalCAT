# 实施计划

## 任务说明

本计划从已冻结的 R/D/T 开始，顺序闭合 TM JSONL 与术语 CSV/v1 的直接导出、独立 ResourcePackage carrier、validate/preview/import/apply/receipt/cold reopen，最后接入 Controller/Qt 并形成供 sync 未来消费的 immutable port。

固定顺序：

```text
Cluster 0 治理/characterization
  → Cluster 1 owner snapshot ports + direct JSONL/CSV + receipt/publication substrate
  → Cluster 2 ResourcePackage manifest/carrier/export/validate
  → Cluster 3 preview/import/apply pending recovery
  → Cluster 4 Controller/Qt/current-source completion
  → future sync consumer
```

每个 Cluster 仅在其全部任务、focused tests、fault matrix、architecture guards、diff check 和累计实施验证通过后进入下一阶段；全部 Cluster 最终压缩为一个 `feat(resource)` 提交。不得把下一 Cluster 的 production 变化抢跑进当前阶段。

## Contract Freeze（实施前）

- [x] 0.0 冻结 R/D/T 一致性
  - Requirements ↔ Design ↔ Tasks 已覆盖 Cluster 依赖、六项持久/语义决策和 ProjectPackage/TMX/provider/live Store 负向边界。

- [x] 0.1 冻结 Requirements
  - 确认本规格独占 JSONL/CSV ResourcePackage container/manifest/profile/transaction，不抽象 ProjectPackage 共同 authority。
  - 确认 TMX/context/provenance/loss report 仍归 `tmx-context-interchange`，首批 profile-set 不接受 TMX。
  - 确认 direct JSONL/CSV 是同 profile payload 的独立发布面，不冒充 ResourcePackage。

- [x] 0.2 冻结 Design 的 v1 持久决策
  - 批准“每包正好一个资源”、strict deterministic `ZIP_STORED`、exact two-member layout 和 512 MiB/1 MiB 限额。
  - 批准 import 只有 `create_new` / `replace_selected`，package 层不 merge。
  - 批准 manifest 不嵌 source-local identity/name/operation receipt，receipt 由本地 durable ledger 保留。
  - 后续若引入跨规格持久格式决策，另建本 spec 的 carrier/transaction ADR；不改写 ADR-018/019。

- [x] 0.3 冻结 Tasks 与 Cluster 边界
  - 批准 owner snapshot port + 通用 receipt/publication 基座 → package export → import/apply pending recovery → UI 的顺序。
  - 批准每 Cluster 独立语义提交、可重放累计验证与未通过即 NO-GO。

### Contract Freeze 完成门

- `spec.json` 三项 `approved=true` 且 `ready_for_implementation=true`。
- 六个持久/语义决策在 Requirements / Design / Tasks 中无待定分支。

## Cluster 0：治理、Characterization 与边界证据

- [x] 1.1 冻结现有 TM export/import 业务面
  - 枚举 `TMMigrationService.export_jsonl()` 的 destination family、SnapshotManifest/receipt ledger、generation/revision、skipped/diagnostic、publication/recovery 与 patch target。
  - 枚举 `import_snapshot()` / `rebuild_from_snapshot()` 对 configured resource identity、stage/seal/activation/rollback/recovery 的调用顺序。
  - 直接 JSONL 产品出口固定为 JSONL+adjacent SnapshotManifest 事务 family；ResourcePackage 私有 stage 复用 exporter，但只封装 JSONL payload并清理 companion，不复用它作为 package manifest。
  - _Requirements: 1.1–1.6, 2.1–2.6, 6.1–6.6, 12.1_

- [x] 1.2 冻结 mixed legacy/v1 Termbase snapshot 事实
  - 枚举 `TermbaseStore` row kind/record id/flags、canonical serialize、list/prepare/commit/recovery 与 LKG/runtime reload seams。
  - 用同时含 legacy/v1、quotes/newlines 的 fixture 冻结字节与语义基线。
  - 证明 Parser 列选择 import 是外部表格 merge，不能作为 snapshot replace reader。
  - _Requirements: 3.1–3.6, 8.5, 12.1–12.2_

- [x] 1.3 冻结资源 Repository/Controller/Qt 生命周期与故障面
  - 枚举 resource id/path/kind/registry create-update-delete、TM lifecycle gate、term commit、runtime graph reload 与 worker busy seam。
  - 冻结 create-new 与 replace-selected 需要的 registry/resource 发布顺序、对抗 fault points 和可恢复状态。
  - _Requirements: 7.4–7.7, 8.1–8.8, 10.1–10.6_

- [x] 1.4 建立 architecture/patch/error/current-source inventory
  - 冻结 production/tests 对 TM/Termbase/resource 公开导入、private patch target、stable code 与调用顺序。
  - 新增负向架构基线：无 ResourcePackage→ProjectPackage、package→JSONL/CSV row grammar、Qt→carrier/Store 依赖。
  - 冻结实现完成时需重跑的 TM fault/acceptance/release、termbase/Qt/architecture source roots。
  - _Requirements: 1.3, 5.6, 11.1–11.5, 12.1–12.7_

### Cluster 0 完成门

- 只有治理/characterization tests，无 production runtime/UI 变化。
- 三个 owner 的发布/恢复边界、直接 JSONL companion 策略和术语 snapshot grammar 均有可执行证据。
- 累计 architecture 与边界证据确认未将 ProjectPackage/TMX/provider/live SQLite 纳入。
- 本 Cluster 作为最终 `feat(resource)` 的治理与 characterization 前置，不单独保留提交。

## Cluster 1：Owner Snapshot Ports、Direct Export 与 Receipt/Publication 基座

- [x] 2.1 建立 ResourcePortability leaf contracts
  - 新增 resource kind/profile/import mode/operation kind、snapshot facts、export outcome、receipt/failure/recovery DTO 与 canonical receipt codec。
  - 冻结通用 durable ledger/publication 合同；receipt 只是操作证据，pending/LKG/stage 只是本地 recovery 事实。
  - exact type、tuple/private copy、digest/count/profile closure 全部 fail closed；contracts 不导入 Store/Qt/ProjectPackage/provider。
  - _Requirements: 1.1–1.6, 4.1–4.6, 9.1–9.7_

- [x] 2.2 建立 TM portable snapshot adapter
  - 以 public `TMMigrationService.export_jsonl()` 作为唯一 producer，将 `ExportReport`/`ExportFailure` 投影为中立 snapshot outcome。
  - 成功必须零 skipped/error，destination digest/count/generation/revision/SnapshotReceipt digest 闭合。
  - adapter 不扫描 SQLite、不调用 `_export_jsonl_row`、不重编码 payload。
  - _Requirements: 2.1–2.6, 9.2–9.4, 12.1_
  - _Depends: 2.1_

- [x] 2.3 建立 Termbase portable snapshot port
  - 在 `TermbaseStore` 增加公开 export/validate/prepare-replace/commit/cold-reopen 面，复用其唯一 mixed row grammar/encoder。
  - 冻结 UTF-8 BOM/LF/CSV quoting、legacy/v1 行与 counts，不暴露 private row parser。
  - 保留既有 CRUD/merge transaction、LKG、locator 和 matching 语义。
  - _Requirements: 3.1–3.6, 8.5, 12.1–12.2_
  - _Depends: 2.1_

- [x] 2.4 实现直接 JSONL 与 CSV/v1 导出事务
  - TM JSONL 直接导出将最终用户 destination 交给 `export_jsonl()`，不在其外再套一层 replace；Application 复证其 destination family/receipt/count/digest 闭合。
  - Termbase CSV/v1 绑定 source baseline 与 destination parent/file，在同 parent dirfd 上 stage/validate/LKG/publish/readback。
  - 保持旧 destination；首次 LKG `None`；unknown target 不自动删除。
  - 在本 Cluster 落地通用 `ResourceReceiptLedger`、artifact publication/LKG 与 path-free pending inventory；冷重开 actual destination 后写入 durable receipt，不完整导出不返回 success。
  - _Requirements: 2.1–2.6, 3.1–3.6, 6.1–6.6, 9.1–9.7_
  - _Depends: 2.2, 2.3_

- [x] 2.5 闭合 direct export fault/cold-reopen matrix
  - 覆盖 source drift、parent/destination replacement、same-bytes/new-inode、stage/fsync/replace/readback/ledger/cleanup 故障，以及重启后 receipt-ready complete/manual recovery inventory。
  - 覆盖 JSONL+adjacent SnapshotManifest destination family 的事务一致性及 package 私有 companion 清理。
  - 覆盖 mixed termbase exact bytes/row facts、TM ExportReport/receipt closure 与 cold process reopen。
  - _Requirements: 6.1–6.6, 9.1–9.7, 12.2–12.4_
  - _Depends: 2.4_

### Cluster 1 完成门

- 从真实 active canonical TM 和 mixed termbase 经目标业务 API 导出/冷重开，不用伪 port 作为唯一 acceptance。
- 旧目标保留、recovery idempotence、receipt ledger 和 architecture guards 通过。
- 本 Cluster 验证通过后继续进入 ResourcePackage carrier，不单独保留提交。

## Cluster 2：ResourcePackage Manifest、Carrier、Export 与 Validate

- [x] 3.1 实现 logical manifest/profile/limits 合同
  - 实现 exact DTO/canonical encoder/duplicate-key-aware decoder、kind/profile/path/count closure。
  - 首批 profile-set 只含 TM JSONL 和 termbase CSV/v1，拒绝 TMX/未知 extension。
  - golden/mutation tests 冻结 key order、lexical JSON、manifest/content digest 和 512 MiB/1 MiB 限额。
  - _Requirements: 1.1–1.6, 4.1–4.6, 9.1–9.6_

- [x] 3.2 实现 strict `localcat-resource-package-zip-v1`
  - 实现 exact two-member deterministic writer 和 raw preflight reader，stored-only/no-ZIP64/no-extra/no-comment/no-descriptor。
  - 从 retained regular-file handle 流式复算 CRC/SHA/count，不 extract/整包 materialize。
  - 不导入、extends 或参数化 `project_package.py`；仅可复用经 guard 证明的无语义 I/O 原语。
  - _Requirements: 5.1–5.6, 12.3–12.5_
  - _Depends: 3.1_

- [x] 3.3 实现 ResourcePackage export
  - 消费 Cluster 1 exact profile payload，只在零 skipped/error 且 owner receipt/count/digest 闭合时生成 manifest/package。
  - 关闭 writer 后用 fresh ResourcePackage reader 冷验证 candidate，再复证 source/destination 并发布。
  - 从 actual destination 冷重开，返回 artifact/content/payload digest 与 profile counts 闭合的 receipt。
  - _Requirements: 2.2–2.5, 3.4–3.5, 5.1–5.6, 6.1–6.6, 9.1–9.7_
  - _Depends: 2.5, 3.2_

- [x] 3.4 实现独立 package validate 与 sealed handle
  - raw carrier -> manifest -> payload digest -> matching owner profile validation 的顺序不可跳过。
  - public report 只返回 schema/profile/digest/count/safe issues，private handle 不可串行化或伪造。
  - TM/Termbase profile validation 不在 package module 重写 row grammar。
  - _Requirements: 4.1–4.6, 5.1–5.6, 7.1–7.3, 12.5_
  - _Depends: 3.2_

- [x] 3.5 闭合 carrier/package export 对抗矩阵
  - 覆盖 local/CD name/CRC/size/offset、gap/overlap、flags/data descriptor/encryption、extra/comment/attrs、prefix/suffix、duplicate/missing/undeclared member。
  - 覆盖 manifest schema/profile/kind/path/digest/count/limits 篡改、source sealed drift 和 destination publication/recovery faults。
  - 证明 same profile+payload bytes 产生 same package bytes，操作/resource-local 随机事实不进 manifest。
  - _Requirements: 4.1–4.6, 5.1–5.6, 6.1–6.6, 12.2–12.5_
  - _Depends: 3.3, 3.4_

### Cluster 2 完成门

- 两个真实 profile 均通过 export→cold validate，package payload 与 Cluster 1 direct bytes exact 对账。
- raw ZIP hostile matrix、发布/LKG/recovery 和 ProjectPackage 分权 guard 通过。
- 本 Cluster 验证通过后继续进入 import/apply，不单独保留提交。

## Cluster 3：Preview、Import/Apply 与 Pending Recovery

- [x] 4.1 建立 sealed preview 与 single-use apply plan
  - 预览绑定 exact source artifact/manifest/payload/profile handle，不 hash-then-reopen。
  - `create_new` 绑定 repository/managed-root baseline；`replace_selected` 绑定 exact ResourceConfig/path/digest 与 owner generation/revision/snapshot。
  - public preview body-safe 且零写，private capability 一次消费，伪造/重放/stale 在 mutation 前拒绝。
  - _Requirements: 7.1–7.7, 8.1–8.3, 9.1–9.6_
  - _Depends: 3.4_

- [x] 4.2 实现 ResourceRepository staged create 与同 kind replace 协调
  - 新资源的 local id/path 由 Repository 分配，在 owner snapshot 发布/冷重开成功前不进 public registry/runtime。
  - replace 只接受用户明选且 kind 一致的资源，不根据 name/path/package source id 自动匹配。
  - registry/resource publication 任一故障都有 exact prior/new artifact 证据与 recovery path token。
  - _Requirements: 8.1–8.3, 8.6–8.8, 9.1–9.7_
  - _Depends: 4.1_

- [x] 4.3 实现 TM/Termbase package apply
  - TM 只调用 snapshot import/rebuild stage/seal/activation/recovery；term 只调用 Store snapshot prepare-replace/commit/recovery。
  - apply 不 merge，不直接复制 SQLite/改 journal，不改 source package。
  - owner publication 后通过真实业务 reader 冷重开，核对 semantic digest/count/row facts，再发布 runtime/receipt。
  - _Requirements: 8.1–8.8, 9.1–9.7, 12.2–12.4_
  - _Depends: 4.2_

- [x] 4.4 复用 C1 ledger 实现 import/apply pending recovery
  - 不重建 receipt exact codec、safe-root atomic persistence 或通用 artifact publication；只在 C1 `ResourceReceiptLedger`/pending inventory 上增加 import/apply phases。
  - 区分未发布、owner已发布但 registry/runtime/receipt 未完成、rollback 已证明与未知人工介入。
  - success receipt 只是操作证据，不传输 pending journal/stage/LKG，不铸造 Store/provider 权限。
  - _Requirements: 6.3–6.6, 8.6–8.8, 9.1–9.7, 11.1–11.3_
  - _Depends: 4.3_

- [x] 4.5 闭合 preview/apply/recovery fault matrix
  - source/package/profile adapter/destination/resource graph/generation/revision/baseline 在 preview 后变化的零 mutation。
  - create-new/replace 的 stage、owner publication、cold reopen、registry/runtime switch、receipt ledger、cleanup 故障。
  - cold process 检查 complete/rollback/manual actions 一次消费、替换后 stale、unknown target 不删除。
  - _Requirements: 7.4–7.7, 8.1–8.8, 9.1–9.7, 12.3–12.4_
  - _Depends: 4.4_

### Cluster 3 完成门

- 真实 TM 与 mixed termbase 均完成 package validate→preview→create/replace apply→cold process reopen→receipt 对账。
- import/apply pending recovery 复用 C1 已批准的 codec/ledger/publication 基座，不出现第二 receipt authority。
- 所有 stale/fault 证明 prior resource/registry/runtime/source package 的保留语义。
- 本 Cluster 验证通过后继续进入产品接入，不单独保留提交。

## Cluster 4：Controller、Qt、Sync Handoff 与 Completion

- [x] 5.1 接入 EditorController typed commands/projections
  - 增加 direct/package export、validate/preview/apply/recovery 入口，只接受 current issued resource identity/graph epoch。
  - 成功后只 reload receipt 指定 destination resource，失败不清空或替换现有 runtime。
  - Controller 不解析 ZIP/manifest/JSONL/CSV 或 owner journal。
  - _Requirements: 7.4–7.7, 8.2–8.7, 10.1–10.6_

- [x] 5.2 接入 Qt 非阻塞导出/导入 UI
  - 在资源 `⋮` 添加 TM JSONL/ResourcePackage 与 term CSV/v1/ResourcePackage 出口；增加全局资源包导入。
  - validate/preview 后显示 kind/profile/counts/warnings，新建或同 kind 替换二次确认。
  - worker/busy/cancel/close/failure 释放完整，反馈不包含资源正文/raw manifest/internal path。
  - _Requirements: 10.1–10.6, 12.1–12.4_
  - _Depends: 5.1_

- [x] 5.3 建立 immutable package/metadata downstream port
  - 只暴露 bounded stream、artifact/payload digest、byte count、schema/profile/kind/counts 与 serializable success receipt。
  - 不实现 provider/network/credentials/encryption/listing/conflict；负向测试证明 consumer 不可绕过 validate/preview/apply。
  - TMX 继续 `PROFILE_UNSUPPORTED`，不给 future profile 留未审批 runtime reader。
  - _Requirements: 11.1–11.5, 12.5–12.7_
  - _Depends: 4.5_

- [x] 5.4 执行 current-source acceptance 与架构攻击
  - 从真实 active canonical TM 和 mixed legacy/v1 termbase 经 Qt/Controller 完成 direct/package export、create/replace import 和 cold reopen。
  - 执行 contracts/profile/carrier/publication/preview/apply/recovery/Controller/Qt/architecture 全矩阵，以及 TM fault/acceptance/release 和 termbase/column-selection 兼容回归。
  - 扫描 public logs/reports/receipts 无正文、raw manifest、absolute path、credential 或 Store internals。
  - _Requirements: 9.1–9.7, 10.1–10.6, 12.1–12.7_
  - _Depends: 5.2, 5.3_

- [x] 5.5 完成治理收尾
  - 只在 final runtime roots 冻结后机械重签 current-source inventory/evidence，不用旧结果纯重签。
  - 同步已实现的 structure/tech/roadmap 事实，不声称 TMX/provider/sync/conflict 已交付。
  - 以累计 diff、faults、cold-reopen receipt 和 ownership 的可重放证据闭合 Feature 验收。
  - _Requirements: 1.1–1.6, 11.1–11.5, 12.1–12.7_
  - _Depends: 5.4_

### Cluster 4 完成门

- 用户可在 Qt 上完成 TM JSONL、term CSV/v1 与两种 ResourcePackage 人工闭环。
- 目标业务 reader 冷重开、receipt 对账、fault recovery 与既有 TM/术语回归通过。
- immutable port 可供 future sync 消费，但仓库中没有 provider/TMX 首轮实现。
- 最终提交：`feat(resource): 闭合 ResourcePackage 可移植事务`。

## 明确禁止

- 在 R/D/T 尚未冻结或任一前置 Cluster 完成门未通过时抢跑后续 production/Qt。
- 从 live SQLite、sidecar、journal、stage 或 backup 直接构建/同步 package。
- 让 ResourcePackage 层解析 TM JSONL row 或 termbase CSV row grammar。
- 让术语 ResourcePackage 通过 Parser 两列列选择 merge 导入。
- 把 ProjectPackage/ResourcePackage 改成共同 manifest/base service/identity/merge authority。
- 将 TMX、context/provenance/loss semantics、provider、sync conflict、Fuzzy 或 CONTEXT UI 抢跑进本规格。
- 用“ZIP 可打开”、“文件已复制”或内存 fixture 代替最终 TM/Termbase 业务 reader 冷重开验收。
