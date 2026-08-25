# 需求文档

## 简介

LocalCAT 已有 canonical TM 的安全 JSONL 兼容导出，也有能完整读取 legacy/v1 混合术语表的 `TermbaseStore`，但产品层没有将这些能力闭合成可验证、可预览、可恢复的人工资源导入/导出事务。若后续同步层直接复制 live SQLite、sidecar、journal 或 stage residue，就会把设备本地 Store authority 与未完成交易一起传输。

本规格建立独立的 `ResourcePackage`：v1 每包携带一个 TM JSONL 或术语 CSV/v1 完整快照，以版本化 manifest/profile、strict deterministic carrier、export/validate/preview/import/apply/receipt 和发布后冷重开闭合人工备份/迁移。直接 JSONL/CSV 与 ResourcePackage 共用对应资源 owner 产生的同一 payload bytes，但直接文件不被宣称为 package。

## 范围边界

- **范围内**：TM 直接兼容 JSONL 导出；术语 managed CSV/v1 直接导出；单资源 ResourcePackage manifest/carrier/profile；导出、冷验证、预览、新建/替换导入、apply、receipt、recovery 与冷重开；Controller/Qt 手工入口；供后续 sync 消费的 immutable bytes/metadata port。
- **范围外**：TMX payload/export/context/provenance/loss semantics；ProjectPackage 及项目身份；live SQLite/sidecar/journal/stage 传输；provider、S3/WebDAV、凭据、加密、remote listing、同步调度/冲突合并；TM Store canonical authority；Fuzzy 资格；TM CONTEXT UI。
- **相邻期望**：`tmx-context-interchange` 未来可在新的 approved profile-set 版本中增加 TMX payload adapter，但不取得 ResourcePackage container/apply authority；`cross-device-sync-plugin` 未来只传 package bytes+metadata 并调用同一 preview/apply 事务。

### Scope Lineage

- **Owning spec**：`language-resource-portability`。
- **Upstream facts**：`TMMigrationService.export_jsonl()` / `ExportReport`、TM snapshot import/rebuild transaction、`TermbaseStore` mixed legacy/v1 grammar/transaction、`ResourceRepository` local identity/path authority。
- **Borrowed lesson, not authority**：`multi-document-project-workspace` C2C 的 sealed source、strict ZIP、preview/apply stale binding、candidate/LKG/readback/recovery 经验；不复用其 manifest 或项目身份。
- **Contract state**：本 R/D/T 已冻结并可实施；本文只描述目标合同，不表示 runtime 已实现。

## 需求

### Requirement 1：独立 ResourcePackage Authority

**目标：** 作为架构维护者，我希望资源包只表达语言资源快照，以便不污染项目、Store 或同步权威。

#### 验收标准

1. The `ResourcePackage` shall 拥有资源 package schema/profile、payload member/digest/count、validate/preview/apply 计划与 resource-operation receipt。
2. The ResourcePackage shall 一次精确封装一个 `translation_memory` 或 `termbase` 快照，不嵌入 project/document/segment、chunk、provider 或 credential 字段。
3. The implementation shall 不与 ProjectPackage 共享 manifest、schema、identity、base package class 或 merge authority；只可复用无语义的 I/O、digest、fsync 与 bounded-stream 原语。
4. The package manifest shall 不包含资源显示名、绝对路径、live SQLite path、canonical store id、operation id 或本地 recovery locator。
5. Where receipt 携带 source/destination local resource id 时，the importer shall 将其仅视为本地操作对账事实，不得用它授权、选择或改写另一设备的 resource authority。
6. The v1 implementation shall 拒绝多资源 bundle、未批准 profile 与任意 extension，不以忽略未知字段的方式“向前兼容”。

### Requirement 2：TM JSONL 直接导出与 Package Payload 同源

**目标：** 作为需要近期人工备份的译者，我希望导出 LocalCAT 可重建的 JSONL，并知道资源包使用的就是同一份快照字节。

#### 验收标准

1. When 导出 active canonical TM 到直接 JSONL 时，the Application shall 调用 `TMMigrationService.export_jsonl()` 并保留其 JSONL+adjacent SnapshotManifest destination family、`ExportReport` / `ExportFailure` 事务、snapshot receipt 与目标保护语义；Core 的成功报告固定为零 skipped/diagnostics，任一不完整结果均由 Core 作为失败恢复 exact prior destination pair。
2. When 导出 TM ResourcePackage 时，the package exporter shall 让同一 TM exporter 向私有、同目录受控 staging 位置产生 JSONL，且只在 `ExportReport` 与实际 digest/count 闭合后才封装 payload。
3. The package exporter shall 将已验证 JSONL 原字节作为 `localcat-tm-jsonl-v1` payload，不得从 live SQLite 另行 scan、不得重排或重编码记录。
4. If 底层 export 未返回零 skipped/diagnostics 的成功报告、receipt/digest/count 不一致或 staging readback 失败，the package exporter shall 不构建/发布一个声称完整的 ResourcePackage；直接导出的 prior JSONL+SnapshotManifest pair 由 Core 的失败事务恢复，package 私有 stage 不影响旧 package 目标。
5. When 直接 JSONL 成功发布后，the exporter shall 以独立 reader 复读实际目标并核对 digest/count；单独复制 JSONL 仍是兼容快照，但不是 ResourcePackage。
6. If TM 尚未形成可导出的 active canonical authority，the Controller shall 返回 body-safe 不可用状态，不从 legacy JSONL 或 live sidecar 自行猜测快照。

### Requirement 3：术语 CSV/v1 快照导出

**目标：** 作为维护 legacy 和自定义匹配术语的译者，我希望导出后不丢失行类型、record id 或匹配标志。

#### 验收标准

1. The `localcat-termbase-csv-v1` profile shall 使用 UTF-8 BOM、LF 和 deterministic CSV quoting，无 header，并只接受 Store 批准的两列 legacy 行与六列 v1 行。
2. When 导出术语资源时，the exporter shall 通过 `TermbaseStore` 公开快照面复读和编码受管记录，保留 row kind、record id、source、target、`match_case` 与 `whole_word`。
3. The exporter shall 不得通过 Parser 列选择 preset、Qt 表格或私有 Store helper 推断术语快照 grammar。
4. When 导出直接 CSV/v1 与 ResourcePackage 时，the two surfaces shall 使用同一份已验证 payload bytes；package 只包装该 payload 与自己的 manifest。
5. If 任何行无效、snapshot 在 stage 期变化、计数不一致或 cold readback 失败，the exporter shall 返回结构化失败并保留旧目标字节。
6. The existing CSV/XLSX 列选择导入 shall 继续表达外部表格转换/merge，不自动改用 snapshot replace 语义。

### Requirement 4：版本化 Manifest、Profile 与 Limits

**目标：** 作为安全审阅者，我希望包内语义是有界闭集，以便未知或恶意输入不会被宽松解释。

#### 验收标准

1. The v1 manifest shall 使用 canonical UTF-8 JSON 与 exact root/resource/payload key set，声明 `localcat-resource-package-manifest-v1`、`localcat-resource-package-zip-v1` 和 `localcat-resource-payload-set-v1`。
2. The v1 profile set shall 只允许 `translation_memory/localcat-tm-jsonl-v1` 与 `termbase/localcat-termbase-csv-v1` 两个 kind/profile 组合，未知或交叉组合 fail closed。
3. The manifest shall 绑定 payload relative path、SHA-256、byte count、record count 与 profile-specific safe counts，不复制记录正文。
4. The v1 parser shall 拒绝 duplicate JSON key、额外/缺失字段、错误 exact type、bool-as-int、NaN/Infinity、非 canonical number/string/order 与未批准 extension。
5. The ResourcePackageLimitProfile v1 shall 在 materialize payload 前执行：artifact 与 total decoded payload 不超过 512 MiB，manifest 不超过 1 MiB，member 数正好为 2，retained safe issues 不超过 256，JSON nesting 不超过 32。
6. The exact manifest schema + carrier profile + payload profile-set triple shall 唯一映射到 `localcat-resource-package-limits-v1`；When 未来提高/降低限额或增加 TMX profile 时，the change shall 通过新 schema/carrier/profile-set triple 与 owning spec 明确冻结，不静默宽化 v1。

### Requirement 5：Strict Deterministic ResourcePackage Carrier

**目标：** 作为人工搬运资源的用户，我希望包是单个可验证文件，以便复制或上传时不会出现部分成员。

#### 验收标准

1. The v1 carrier shall 为 classic single-disk deterministic ZIP，仅使用 `ZIP_STORED`，成员顺序为 `manifest.json` 后跟该 profile 的唯一 payload path。
2. The carrier shall 拒绝 ZIP64、compression、encryption、data descriptor、extra field、archive/member comment、multi-disk、prefix/trailing bytes、duplicate member、undeclared/missing member、symlink/executable 属性。
3. The carrier shall 固定 member timestamp、flags、creator/version、permissions 与 UTF-8 path bytes，并拒绝绝对路径、`..`、NUL、backslash、空 segment、NFC/casefold collision 或非 canonical member path。
4. When 验证 package 时，the reader shall 先做 raw envelope/central-directory/local-header preflight，再从 retained regular-file handle 以小 buffer 流式复算 byte count/CRC/SHA-256，不 extract member 到用户路径。
5. For 同一 canonical payload/profile，the exporter shall 产生相同 package bytes；operation id、destination、resource display name 与随机 snapshot id 不得进入 package content。
6. The ResourcePackage carrier implementation shall 独立于 ProjectPackage reader/writer；不得通过继承或参数化 ProjectPackage manifest 实现本包。

### Requirement 6：导出发布、LKG 与 Recovery

**目标：** 作为已有一份成功备份的用户，我希望新导出失败时旧文件仍可用。

#### 验收标准

1. When 导出直接 payload 或 ResourcePackage 时，the exporter shall 在与目标同一已绑定 parent directory 中创建 exclusive candidate，完成 fsync、关闭、独立冷验证后才发布。
2. Before candidate 创建与 publication，the exporter shall 绑定并复证 target parent device/inode 以及 destination absent 或 exact regular-file identity/digest，拒绝 symlink、hardlink、special file 和 parent replacement。
3. If destination 已存在，the exporter shall 在 replace 前建立同一已绑定 parent 中的可验证 LKG；首次导出的 LKG 明确为 `None`。
4. If 故障发生在 publication 前，the exporter shall 证明 destination 未变；If publication 后验证失败，it shall 只在证明 target 仍为本 operation candidate 时恢复 exact prior destination，否则返回 recovery-required 而不报成功。
5. When publication 完成时，the exporter shall 独立冷重开实际 destination，核对 artifact/payload digest、profile 与 counts，再清理 owned LKG 并写入 durable receipt。
6. When 冷启动发现 pending export receipt 时，the recovery service shall 只完成已证明 receipt-ready 的事实；无法从 path-free pending evidence 判定的外部目标进入 manual-required，未知 target 字节不得被自动删除或覆盖。

### Requirement 7：只读 Validate 与 Body-safe Preview

**目标：** 作为准备导入资源的用户，我希望在修改本地资源前看到包的类型、数量与风险。

#### 验收标准

1. When `validate_resource_package(source)` 运行时，the validator shall 建立 rooted/no-follow sealed source，验证完整 carrier/manifest/member/digest/profile/limits，且不修改 source 或任何本地资源。
2. The validator shall 将 retained payload stream/path 交给 matching TM/Termbase profile owner 复读，核对 record/profile-specific counts，不在 ResourcePackage 模块建立第二套 JSONL/CSV row grammar。
3. When validation 成功时，the service shall 返回 frozen `ResourcePackageValidationReport`，至少含 artifact/payload digest、schema/carrier/profile、resource kind、counts 和 safe issues。
4. When 用户选择 `create_new` 或明选 `replace_selected` destination 时，the preview shall 绑定 sealed source identity/digest、destination resource id/kind/path identity/current digest，以及 TM generation/revision 或 termbase baseline digest。
5. The public preview shall 只显示 resource kind/profile、record/legacy/v1/skipped/warning counts、create/replace mode、destination 是否存在与 blocking reasons，不包含记录正文、provenance 值、绝对路径或 raw manifest。
6. The preview shall 只是一次性 capability，不可串行化为可重放授权 token；cancel 不修改 destination。
7. If source/destination/resource lifecycle/baseline 在 preview 后变化，the apply shall 以 stable stale code 在首次本地 mutation 前拒绝。

### Requirement 8：Import/Apply 完整快照事务

**目标：** 作为恢复或迁移资源的用户，我希望导入成功后得到完整快照，任何失败不留下半套新权威。

#### 验收标准

1. The v1 apply mode shall 只允许 `create_new` 或 `replace_selected`；package 层不提供 append、merge、source-LWW 或自动冲突解决。
2. When `create_new` apply 运行时，the Application shall 由 `ResourceRepository` 分配新的本地 resource id/path，并使资源 snapshot 发布与 registry 可见性形成一个可恢复事务；package 中的 source id 不是本地 id。
3. When `replace_selected` apply 运行时，the Application shall 要求 destination kind 与 package kind 一致，并在首次 mutation 前复证 preview 的 exact local resource/baseline/lifecycle facts。
4. For TM payload，the apply shall 调用 TM owner 的快照 import/rebuild stage/seal/activation/rollback/recovery 面，不直接复制或打开 live SQLite。
5. For termbase payload，the apply shall 调用 `TermbaseStore` 公开 snapshot validate/prepare-replace/commit/recovery 面，不经 Parser 两列 merge 丢失 v1 事实。
6. If profile validation、stage、publication、registry switch、runtime reload 或 cold reopen 任一步失败，the apply shall 保留或恢复 prior resource/registry/runtime；无法证明时返回 recovery-required 而不报成功。
7. When apply 成功时，the service shall 从 final local resource 经对应业务 owner 冷重开，核对 payload semantic digest、record/profile counts 与新 local authority 状态。
8. The source ResourcePackage shall 保持不变；apply 不将本地 generation、journal、sidecar 或 stage 回写进 package。

### Requirement 9：Structured Report、Receipt 与 Cold Reopen

**目标：** 作为用户与后续 sync consumer，我希望每次操作都有可对账的有界证据。

#### 验收标准

1. Export/validate/preview/import/apply/recovery shall 返回 exact frozen DTO、tuple 集合与 stable enum/code；只有成功 direct/package export 与成功 package apply 写入 versioned canonical JSON receipt，validate/preview 为只读报告，recovery 完成或清理原 operation receipt/pending 事实而不另造语义操作。
2. A successful `ResourceOperationReceipt` shall 绑定 receipt schema、operation kind/id、resource kind/profile、package artifact digest（如适用）、payload digest、source/destination before/after digest、record/profile counts、warnings 与 durable outcome。
3. For TM export，the receipt shall 与底层 `ExportReport` 的 generation/revision/snapshot receipt digest 对账；For termbase export，it shall 对账 source baseline digest 与 legacy/v1 counts。
4. If 任何记录被跳过、验证失败或无法冷重开，the operation shall 返回 failure/recovery report，不签发成功 receipt。
5. The receipt shall 不包含 source/target/context/provenance 正文、术语文本、绝对路径、credential、device key、live store path 或 recovery artifact path。
6. When 冷启动重读已发布的直接文件/package/final local resource 时，the service shall 能用 receipt 的 digest/count/profile 证明同一成功事实；receipt 不授予 provider、merge、Fuzzy 或 canonical-store 权限。
7. The Application shall 以原子、可冷重开的本地 receipt ledger 保留成功/需恢复的操作事实，且不将 stage residue 误作为 receipt。

### Requirement 10：Controller / Qt 人工可达面

**目标：** 作为桌面端译者，我希望从资源页完成导出、预览和导入，并看到真实的数量/恢复结果。

#### 验收标准

1. When TM resource 可导出时，the Qt resource menu shall 提供“导出兼容 JSONL”和“导出资源包”；When termbase 可导出时，it shall 提供“导出 CSV/v1”和“导出资源包”。
2. The Qt surface shall 提供资源包导入，先异步 validate/preview，再要求用户选择新建资源或明选同 kind 替换目标并二次确认。
3. The Qt shall 只通过 `EditorController` frozen commands/projections 消费资源事务，不直接读 ZIP/JSONL/CSV、Store、manifest、journal 或 receipt ledger。
4. Export/validate/import shall 在非 GUI 线程运行，与现有资源导入共用冲突操作 busy gate；cancel 与 worker failure 必须释放资源并恢复交互。
5. When 操作完成时，the UI shall 显示 exported/imported/legacy/v1/skipped/error counts、profile、receipt 状态与 retry/recovery 建议，不回显记录正文或内部路径。
6. If 导出失败时，the UI shall 明确说明旧目标是已证明保留还是需恢复；不得以单一“导出完成”掩盖 skipped/error/recovery-required。

### Requirement 11：Sync Port、TMX 与其他边界

**目标：** 作为后续同步/互操作规格维护者，我希望只消费已批准产物，不重新解释本地资源。

#### 验收标准

1. The downstream transfer port shall 只暴露 immutable package byte stream、artifact digest、byte count、schema/profile 与 serializable receipt metadata，不暴露 resource parser、SQLite handle、TermbaseStore 或 local path。
2. The sync/provider layer shall 只执行 list/get/put/delete bytes+metadata，并将收到的 bytes 交回同一 validate/preview/apply 事务，不直接替换 active resource。
3. The package/export surface shall 不枚举、读取或传输 live SQLite、WAL/SHM、sidecar、activation/schema journal、stage、backup 或 recovery residue。
4. The v1 validator shall 拒绝 TMX payload/profile；未来 TMX 只能由 `tmx-context-interchange` 冻结 payload grammar/context/provenance/loss report，再经本规格批准的新 profile-set adapter 接入。
5. The ResourcePackage shall 不计算或签发 Fuzzy 资格，不改变 CONTEXT availability/UI，不声称未来 TMX 导出无损。

### Requirement 12：兼容、安全与完成证据

**目标：** 作为现有 LocalCAT 用户和规格审批者，我希望资源可移植性不回归既有导入/检索/编辑，且用真实业务 reader 证明完成。

#### 验收标准

1. The implementation shall 保持现有 TMX import、TM Exact/Context/Fuzzy availability、JSONL migration/refresh/export/recovery、术语 CSV/XLSX 列选择 merge、legacy matching、v1 CRUD 和资源 hot reload。
2. The acceptance shall 使用至少一个真实 active canonical TM 和一个同时含 legacy/v1 行的真实术语表，分别完成 direct export 与 package export→cold validate→preview→create/replace apply→cold reopen。
3. The fault matrix shall 覆盖 source/destination parent replacement、hardlink/symlink/special file、manifest/member/CRC/digest/count/profile 篡改、preview 后 source/destination 变化、stage/fsync/replace/readback/receipt-ledger/cleanup/recovery 故障。
4. When 验收失败路径时，the tests shall 证明旧导出文件、prior TM generation/store、prior termbase bytes、registry/runtime 与 source package 的各自保留事实。
5. The architecture guards shall 证明 ResourcePackage 不读 JSONL/CSV record grammar、不读 live SQLite，Qt/Controller 不读 carrier，ProjectPackage/ResourcePackage 没有共同 authority。
6. The implementation shall 全程本地执行，不向网络发送 package、资源、receipt 或诊断。
7. If Requirements/Design/Tasks 一致性、定点故障、冷重开、架构边界或 current-source regression 任一未通过，the feature shall 保持 NO-GO，不得供 sync 消费。

## 非功能约束

- 所有跨层合同使用 frozen dataclass、exact built-in type、tuple 和 bounded copies；bool 不冒充 int。
- 不可信 artifact 使用 retained descriptor/sealed bytes、checked addition 和小 buffer 流式处理；不 extract 到用户目录，不整包 materialize 到内存。
- 未知 schema/profile/version/member/code 组合、摘要不符、stale 或发布不确定均 fail closed。
- public error/report/log 只携 stable code、opaque local id、digest、profile、enum 与 nonnegative counts；不包含资源正文、raw manifest、绝对路径或 OS exception string。
