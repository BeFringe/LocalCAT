# 实施计划

## 任务说明

> **WA-04 Windows compatibility amendment**：以下 `a` 后缀任务只把 ResourcePackage/direct artifact 的 root、lock 与 publication 接到 ADR-020；payload/profile、Repository 与 receipt authority 不变。

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
  - 以 public Core JSONL exporter 作为唯一 producer，将 `ExportReport`/`ExportFailure` 投影为中立 snapshot outcome；direct 入口为 `export_jsonl()`，package 的 prepared 生命周期由 6.3 接入。
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

- [x] 2.4a 将 direct artifact source/destination 接入平台端口
  - JSONL destination family 与 CSV/v1 publication 在 Windows 上使用 retained root/parent binding、`ProcessFileLock` 与 `BoundDirectoryPublisher`；不得绕过 owner snapshot/receipt 闭合。
  - _Amendment: WA-04_
  - _Depends: 2.4, ADR-020, WA-01, WA-06, windows-platform-enablement 2.1_

- [x] 2.5 闭合 direct export fault/cold-reopen matrix
  - 覆盖 source drift、parent/destination replacement、same-bytes/new-inode、stage/fsync/replace/readback/ledger/cleanup 故障，以及重启后 receipt-ready complete/manual recovery inventory。
  - 覆盖 JSONL+adjacent SnapshotManifest destination family 的事务一致性及 package 私有 companion 清理。
  - 覆盖 mixed termbase exact bytes/row facts、TM ExportReport/receipt closure 与 cold process reopen。
  - _Requirements: 6.1–6.6, 9.1–9.7, 12.2–12.4_
  - _Depends: 2.4_

- [x] 2.5a 扩展 Windows identity、锁与 durability fault matrix
  - 覆盖 reparse/hardlink/multi-link、parent/destination replacement、锁竞争/owner exit、replace/readback crash 与 cold recovery；外来目标不删不覆盖。
  - _Amendment: WA-04_
  - _Depends: 2.4a, 2.5, windows-platform-enablement 3.7_

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

- [x] 3.3a 集成 ResourcePackage 成功终结与 Windows 输出锁收尾
  - 在发布终端证明及 owned LKG 清理之后、释放输出锁之前闭合原成功回执；终结回调至多执行一次，原 package 返回路径不重复提交回执，receipt/pending 格式和异常语义不变。
  - 只有该次 package 操作完整终结后才请求可选平台回收；ledger/readback/terminal 或原恢复失败保留锁，收尾本身无法完成不推翻已经闭合的成功结果。
  - 完成时，三种既有 payload profile 的无竞争 Windows 资源包导出不遗留 coordination lock；最终 package、receipt 对账与冷验证结果保持不变。direct CSV/JSONL、导入、payload stage 与 POSIX 路径不触发本能力，且本条不声称清理 JSONL 等 payload 临时正文。
  - 回归覆盖单次 commit、终结回调异常、平台占用/无法证明结果、原恢复事实保留及公开 outcome 不变。
  - _Requirements: 6.5, 6.6, 6.7, 6.8, 9.1, 9.7_
  - _Boundary: ResourceArtifactSaveService 与 ResourcePortabilityService 导出终结集成_
  - _Depends: 3.3, 3.4a, windows-platform-enablement 3.3a, ADR-027_

- [x] 3.4 实现独立 package validate 与 sealed handle
  - raw carrier -> manifest -> payload digest -> matching owner profile validation 的顺序不可跳过。
  - public report 只返回 schema/profile/digest/count/safe issues，private handle 不可串行化或伪造。
  - TM/Termbase profile validation 不在 package module 重写 row grammar。
  - _Requirements: 4.1–4.6, 5.1–5.6, 7.1–7.3, 12.5_
  - _Depends: 3.2_

- [x] 3.4a 以 rooted sealed handle 绑定 ResourcePackage validate/publication
  - Windows validator 从 retained artifact handle 完成 carrier→manifest→payload→owner profile 验证，export publication 复用同一 bound parent；不按路径 hash 后重开。
  - _Amendment: WA-04_
  - _Depends: 3.4, 2.4a, windows-platform-enablement 2.1_

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

- [x] 4.4a 将 package apply/Repository publication 接入 Windows 恢复协议
  - create/replace 在同一 repository binding 与进程锁下协调 owner publication、registry/runtime switch、receipt 与 pending recovery；未知阶段保持 prior resource 和人工恢复事实。
  - _Amendment: WA-04_
  - _Depends: 4.4, 3.4a, WA-06, windows-platform-enablement 3.7_

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

- [x] 5.4a 执行 Windows direct/package hostile 与业务 journey
  - 从真实 active TM 和 mixed termbase 完成 direct/package export、validate、create/replace apply、冷重开，并重放 source/destination replacement、锁与 crash matrix。
  - _Amendment: WA-04_
  - _Depends: 2.5a, 4.4a, 5.4, WA-01, WA-06, windows-platform-enablement 3.7_

- [x] 5.5 完成治理收尾
  - 保留当时 final runtime roots 的 current-source 验收；后续以直接架构及业务测试验证受影响合同，不用旧结果重签源码快照。
  - 同步已实现的 structure/tech/roadmap 事实，不声称 TMX/provider/sync/conflict 已交付。
  - 以累计 diff、faults、cold-reopen receipt 和 ownership 的可重放证据闭合 Feature 验收。
  - _Requirements: 1.1–1.6, 11.1–11.5, 12.1–12.7_
  - _Depends: 5.4_

- [x] 5.5a 闭合 frozen ResourcePackage 可见性与消费合同
  - 在普通候选从 non-repository CWD 执行资源 export/validate/preview/import/apply 与 cold reopen，必要 profile 数据来自普通 bundle，不依赖 checkout 或源码证明副本；TMX package 仍 export-only，资源迁移不迁移 Fuzzy 资格。
  - 实际候选验证数据保护/失败反例，按平台 Requirement 12 复用未变 owner 深入证据并重验触发范围；退出时交付最终 Qt 旅程所需的 owner port 和证据，不让 Qt/Controller 取得 carrier/Store 权威。
  - _Amendment: WA-04_
  - _Delivery phase: frozen post-build_
  - _Depends: 5.4a, ADR-028, windows-platform-enablement 7.2；涉及 Fuzzy 资格的消费另需 windows-platform-enablement 7.3_

### Cluster 4 完成门

- 用户可在 Qt 上完成 TM JSONL、term CSV/v1 与两种 ResourcePackage 人工闭环。
- 目标业务 reader 冷重开、receipt 对账、fault recovery 与既有 TM/术语回归通过。
- immutable port 可供 future sync 消费，但仓库中没有 provider/TMX 首轮实现。
- 最终提交：`feat(resource): 闭合 ResourcePackage 可移植事务`。

## 架构验证维护

- [x] 5.6 用直接架构与行为断言替代本 owner 的静态源码快照
  - 读取实际 owner 源码检查禁止依赖，使用静态、别名和字面量动态导入反例；保留身份、序列化、权限和发布的既有业务测试。
  - 删除本 owner 的机械快照、专用生成器及跨 JSON 摘要消费；历史执行报告保留原字节，按原 Git 范围解释引用。
  - 与 Multi-Document、Chunk、ResourcePackage、TMX 的对应维护项组成一次集成，逐 owner 收束消费者；Parser 架构测试只同步已删除工具和源码枚举，不放宽 owner 规则。
  - 退出条件：本 owner 直接测试、Parser 源码枚举及已删除工具分类检查通过，活动消费者不再要求旧快照；fixture、Gate 与运行时兼容性依据保持不变。
  - _Requirements: 12.4、12.5、12.8_
  - _Boundary: 本 Spec 测试/合同及其静态快照、生成器；跨 owner 仅处理这四项共同的快照消费依赖_

## POSIX TM 资源包事务与失效分类

本组消费共享平台 Task 11.0 的非并行治理闭合前置：治理 owner 提供唯一 ADR-033 治理提交及开发基线继承关系，四个 owning Spec 的范围差异一致后再进入实现。Core 的家族生命周期与 v2 同对象技术门沿下列依赖先闭合；本组不重新建立治理任务或把技术门等同于产品完成。

- [x] 6. 贯通 POSIX 资源包事务与终态

- [x] 6.1 集成 POSIX 包 create/replace 与同一资源 owner 的终态
  - 资源端口消费 Core 提供并由调用方注入的原家族会话，在首次 source 发布前完成家族准入与排空；子阶段及回调只借用外层操作，不通过新建 engine 或路径重绑清除失效。真实 owner 的端口与事务接线属于本任务，runtime 与 port 的实际 composition 由 UI 10.1 消费该接缝完成。
  - 在 owner 仍有效时闭合 publication、registry callback、既有 runtime callback 接缝、receipt 与 final local resource 冷重开；source package 只作只读输入，不复制 live SQLite 或恢复文件。
  - 完成时，真实 create/replace 返回的 payload semantic digest、counts、新 local authority 与 registry/receipt 一致；无法证明的发布或重开结果返回 recovery-required，保留可确认恢复事实且不签发成功。
  - 保留原 immutable artifact 与同一 validate/preview/apply 入口，跨设备交付仍由 RPY 6.2/6.3 闭合。
  - _Boundary: ResourcePortabilityService 与 TM snapshot port 的 owner 事务集成；Repository/ReceiptLedger 仅限受影响终态接缝_
  - _Depends: windows-platform-enablement 11.0, tm-storage-retrieval-index 10.6, tm-storage-retrieval-index 10.9, tm-storage-retrieval-index 10.10_
  - _Requirements: 7.7, 8.3, 8.4, 8.6, 8.7, 9.4, 9.6, 11.1, 11.2, 11.3_

- [x] 6.2 验证包安全、正常失败与资源漂移分类
  - 通过注入真实 Core owner 的 ResourcePort 入口验证包 create/replace、callback、receipt 与独立冷重开；支持环境中的故障继续证明 prior resource、registry/runtime callback 与 source package 的各自保留事实。本任务覆盖资源端口集成，实际 runtime/port composition 及完整 Application 消费由 UI 10.1/10.2 闭合。
  - 保留 closed grammar、路径穿越、链接/special file、摘要/计数/profile、preview stale 与 foreign artifact 拒绝；首次 mutation 前 stale 保持零修改，不能借在线替换边界弱化输入保护。
  - 在真实 builder 入口 rename 原 parent 并建立同字节 clone 后继续实际代码，另覆盖首次 mutation、reconnect、应用 publication 与终态检查；完成时原/新目录实际内容、callback、registry 与 receipt 证明不接管副本、不报告未经证明的成功。
  - 在途失效分别记录 prior 可证、已提交和 unknown/recovery-required；活动 TM 事务内部外部目录替换另列边界实验，记录实际副作用与检测后停止，不要求全时序零副作用。
  - 完成时，导出目标与术语发布原合同保留，R/D/T、故障和 cold reopen 任一不闭合仍阻断 sync 消费。
  - _Boundary: ResourcePortability 包安全、owner 集成及恢复验证_
  - _Depends: 6.1, tm-storage-retrieval-index 10.11_
  - _Requirements: 7.7, 8.6, 8.7, 12.3, 12.4, 12.7_

- [x] 6.3 闭合 JSONL 包私有 stage 的消费与收尾
  - TM snapshot port 获取 Core prepared 生命周期，完成 facts 投影及失败释放；Resource 在 candidate 完整校验后释放 JSONL、companion 和目的 lease，再复证来源、进入目标 publication；持 prepared 期间不重入 canonical owner。
  - 完成时，正常出包及封包前失败不遗留本次正文/companion；清理失败不发布目标，原主异常、旧目标和未知对象保留，已清理与仍保留项不混淆。
  - 通过真实 canonical TM 验证成功、port/source/candidate 校验故障、清理失败及同资源同输出目录的双操作锁顺序，复核 direct JSONL 与既有 package receipt/recovery 合同；受影响 source/frozen 出口进入后续交付验证。
  - _Boundary: TM snapshot port 与 ResourcePackage JSONL export 编排；不新增 JSONL producer、平台操作或冷端清扫_
  - _Depends: 6.2, tm-storage-retrieval-index 10.10a_
  - _Requirements: 2.2, 2.3, 2.4, 9.7, 12.3, 12.4, 12.7_

## 明确禁止

- 在 R/D/T 尚未冻结或任一前置 Cluster 完成门未通过时抢跑后续 production/Qt。
- 从 live SQLite、sidecar、journal、stage 或 backup 直接构建/同步 package。
- 让 ResourcePackage 层解析 TM JSONL row 或 termbase CSV row grammar。
- 让术语 ResourcePackage 通过 Parser 两列列选择 merge 导入。
- 把 ProjectPackage/ResourcePackage 改成共同 manifest/base service/identity/merge authority。
- 将 TMX、context/provenance/loss semantics、provider、sync conflict、Fuzzy 或 CONTEXT UI 抢跑进本规格。
- 用“ZIP 可打开”、“文件已复制”或内存 fixture 代替最终 TM/Termbase 业务 reader 冷重开验收。

## Implement Notes

- Task 6.2：晚期 source terminal 失败后，公开冷 COMPLETE 可闭合已发布的新 authority 与原 import receipt/pending；没有持久归属定位的遗留私有 source 备份仍会保留，`inspect_resource_portability_recovery()` 不是残留文件枚举入口。
- Task 6.1：Core 完整取消返回后、Resource `abandon` 前中断时，既有 ledger 不保留 outer 收尾与该 operation id 的完成关联；冷端即使观察到 source 等于 before、canonical 可查询，仍保留 pending/manual-required。实际应用恢复提示须区分该窗口与同次完整失败已正常结束、可重新预览重试的情况。
- `2.4a`：CSV/v1 direct publication 在 owner receipt callback 返回前保持 `PendingPublication`；ambiguous replace 保留 LKG/candidate。
- `3.4a`：`SealedResourcePackage.reprove()` 同时复证 retained file 与 pathname/parent entry identity。
- `4.4a`：Termbase create在`PendingPublication`存活时只推进owner durable READY/registry事实，terminal reproof成功后才完成receipt ledger；delete在同一owner lock内重读registry并经rooted identity/unlink seam发布；Termbase commit复证live source identity。
