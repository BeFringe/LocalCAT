# Brief: language-resource-portability

## Problem

LocalCAT 的 Core 已能通过 `TMMigrationService.export_jsonl()` 导出兼容 JSONL 并返回 `ExportReport`，但 Controller/Qt 没有对应的产品入口；术语资源也没有经批准的 CSV/v1 出口。若跨设备同步直接复制 canonical SQLite、sidecar、journal 或 stage residue，远端传输将意外继承本地 Store authority 与未完成事务状态。

## Desired Outcome

LocalCAT 为 TM 和术语资源提供可验证、可冷重开、原子发布的人工导出/导入闭环：

- TM 以 LocalCAT 兼容 JSONL profile 导出；
- 术语表以 CSV/v1 profile 导出；
- 每次操作返回结构化数量、跳过、错误、digest 与 receipt；
- 不完整导出不覆盖旧文件，成功产物必须通过冷重开验收；
- 后续同步只消费 Core/Application 批准的 resource package/receipt。

## Authority Boundary

`ResourcePackage` 与 Multi-Document 的 `ProjectPackage` 是两个不同的语义 authority，不抽象成共同 package 权威：

- `ProjectPackage` 拥有 Project/Document/Segment 身份、项目 member、source reconciliation、项目保存与手工包导入导出；
- `ResourcePackage` 只拥有 TM/术语资源 profile、记录数量、资源 digest、导入/导出 receipt 与资源级失败语义；
- 两者未来可复用原子写、digest、preview 和 receipt 的实现原语，但不共享 manifest schema、身份或合并规则。

## Scope

- **In**: Controller/Qt 的 TM 兼容 JSONL 导出；术语 CSV/v1 导出；版本化 resource manifest/profile；结构化 preview/report/receipt；同目录临时文件+原子替换；旧文件保留；冷重开验收；安全 import/apply 事务；供同步层消费的 immutable bytes/metadata port。
- **Out**: TMX export/context/provenance；复制 live SQLite/sidecar/journal/stage；云 provider、凭据、加密、远端 listing 或冲突调度；ProjectPackage；TM Store 模块拆分；Fuzzy 设备资格。

## Existing Contract Reuse

- TM JSONL 导出复用已实现的 `TMMigrationService.export_jsonl()` 与 `ExportReport`，不重写 canonical scan grammar。
- 术语出口以 `TermbaseStore` 的受管 v1/legacy 记录事实为输入，不从 Qt 或 Parser preview 中推断运行时权威。
- `termbase-column-selection-import` 的显式列选择只是 import consumer 事实，不自动成为 export schema。
- `tmx-context-interchange` 独占可选 TMX profile 的语法、context/provenance 取舍、损失报告与跨 CAT 互操作；若该 profile 后续获批，它接入本规格拥有的 ResourcePackage container/preview/apply/receipt，不取得 ResourcePackage 容器或资源事务 authority。本规格首轮只闭合 TM JSONL 与术语 CSV/v1，不以 JSONL 冒充 TMX，也不抢跑 TMX profile。

## Promotion Timing

`multi-document-project-workspace` Cluster 2 已冻结可借鉴的 manifest、preview、receipt 与 failure-semantics 实现经验。本规格的独立 Requirements/Design/Tasks 已冻结，可进入实现；后续只借鉴 ProjectPackage 已验证的无语义实现原语，不继承其身份、manifest 或 schema authority。

该闭环必须在 `cross-device-sync-plugin` 开始传输 TM/术语资源之前完成，但不阻塞 Multi-Document 本身实施。

## Downstream Contract

```text
manual resource export/import
        ↓
versioned ResourcePackage + preview + receipt
        ↓
S3/WebDAV provider transports the same immutable package
        ↓
the same resource import/apply transaction
```

同步 provider 只能 list/get/put/delete bytes+metadata，不解析 TM/术语记录，不签发 Fuzzy 资格，不将远端变成资源 authority。
