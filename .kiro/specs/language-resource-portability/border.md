# language-resource-portability 设计红线

## 当前阶段

本规格的 Requirements / Design / Tasks 已冻结并可实施。实现必须按 Cluster 顺序闭合 owner snapshot、直接导出、ResourcePackage、导入事务与产品入口；任一完成门未通过时不得把后续能力声明为完成。

## 设计要求

- 本规格独占 TM JSONL 与术语 CSV/v1 的 `ResourcePackage` container/manifest/profile、export/validate/preview/import/apply/receipt/cold reopen。
- TM 直接 JSONL 兼容导出必须复用 `TMMigrationService.export_jsonl()`；ResourcePackage 中的 TM payload 必须是该导出器生成并验证的原字节，不得二次解析/重编码。
- 术语 CSV/v1 payload 必须保留 `TermbaseStore` 已管理的 legacy/v1 行种类、record id 和匹配标志；不得借 Parser 的两列导入丢失 v1 事实。
- ResourcePackage v1 是单资源、不可变快照；导入只允许新建本地资源或完整替换用户明选资源，不在 package 层做记录级 merge/LWW。
- preview 只读、不授权 apply；apply 必须消费同一个一次性已验证计划，并在首次本地资源变更前复证 source package 和 destination baseline。
- 导出必须先完整 stage/validate/cold readback，再以单一发布点替换目标；任何记录被跳过、错误、不完整或无法证明的清理都不得覆盖旧成功文件。
- 导入 apply 必须委托 TM/Termbase 既有 owner 的公开交易面；ResourcePackage 层不得直接替换 live SQLite、sidecar、journal、stage 或 canonical store generation。
- 成功只能在发布后以目标业务 reader 冷重开并复证 digest/count/profile 后声称；receipt 只证明操作事实，不铸造 TM Store、provider、sync 或 Fuzzy authority。
- ProjectPackage 与 ResourcePackage 不共享 manifest/schema/base class/identity/merge authority；最多复用无语义的 dirfd、fsync、digest、bounded stream 等底层原语。
- `tmx-context-interchange` 独占 TMX payload grammar、context/provenance、loss report 与跨 CAT 互操作；本规格首轮不允许 TMX profile。
- provider/S3/WebDAV、凭据、加密、remote listing、冲突调度与语义合并均在范围外；后续 sync 只能传 bytes+metadata 并调用同一 import/apply 事务。

## Critical Path

```text
Cluster 0 characterization + carrier/profile 门
  → Cluster 1 资源快照、直接 JSONL/CSV 导出 + 通用 receipt ledger/publication/recovery 基座
  → Cluster 2 ResourcePackage logical/physical export + cold validate
  → Cluster 3 preview/import/apply pending recovery + cold reopen
  → Cluster 4 Controller/Qt + current-source completion
  → sync 才可消费 immutable package/receipt
```

隐性依赖：Cluster 2 的 TM payload 验收依赖 Cluster 1 真正跑过 `TMMigrationService.export_jsonl()` 业务面；Cluster 3 的 apply 验收依赖 TM/Termbase owner 已能将同一 payload 作为完整快照发布并冷重开，不能用“ZIP 可打开”或“文件已复制”代替业务验收。

阶段约束：Cluster 1 必须先闭合通用 receipt exact codec、durable ledger、artifact publication/LKG 与 path-free pending inventory；Cluster 3 只增加 import/apply pending-operation recovery，不建第二 ledger/publication authority。

## 验证锚定

```text
验证目标：从一个真实 canonical TM 和一个同时含 legacy/v1 行的真实术语表导出，经 cold validate、preview、apply 到新资源和明选替换资源，再冷重开后，记录数、字节摘要、TM 快照事实、术语行种/标志与 receipt 完全对账，故障路径的旧资源与旧导出文件不变。
这依赖于：直接导出与 package payload 同源、strict carrier、sealed preview、destination stale gate、store-owned publication 和发布后业务冷重开。
```
