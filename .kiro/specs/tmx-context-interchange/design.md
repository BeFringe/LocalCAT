# 设计文档

## 架构

```text
Managed TM owner ── complete canonical snapshot ─┐
Workspace owner ── session view + universe ──────┼─> TMX export coordinator
Chunk owner ── one exact scope projection ───────┘       │
                                                         ├─> TMX profile/writer/loss report
                                                         ├─> direct artifact publisher
                                                         └─> TMX payload handler ─> ResourcePackage owner

Parser TMX reader ─> prop-preserving ResourceRecord ─> semantic TM importer ─> canonical TM owner
```

## Governance Impact

### Windows Compatibility Amendment WA-05

- **ADR mapping**：follow ADR-020/022/025。TMX 保留 payload grammar、scope/loss、canonical writer 与 direct receipt；Parser 提供 sealed source，platform 提供 `BoundDirectoryPublisher`，packaging 提供 trusted bundle root。
- **Source/writer**：import 不新增 Windows XML reader；direct publication 由 TMX-owned state machine 消费 rooted authority、bound candidate 与 process lock，owner仍负责 locale/scope/canonical byte validation和recovery classification。
- **Recovery evidence**：journal 分为 armed/staged/receipt-ready；success 顺序固定为 target retained readback、receipt-ready journal durable commit、`PendingPublication` terminal namespace reproof。process termination 后以 fresh rooted digest、LKG 与 Parser cold validation分类 prior/after事实，durability遵循共享 publisher contract。
- **Frozen journey**：packaged import/export 从 trusted source/bundle authority 获取声明的 fixtures/resources，不从 CWD 或 checkout 推断；ResourcePackage profile仍通过既有 handler边界组合。
- **Verification**：hostile source zero mutation、canonical byte/reopen、target-before preservation、publish/readback/restart recovery 与 clean-user packaged journey全部使用真实业务 reader/writer。

### ADR-027：Direct TMX 输出锁收尾

- **Scope amendment**：已批准；仅对 Windows direct `.tmx` 的三个既有 scope 复用平台输出锁闲置回收。既有 TMX payload/scope/journal/receipt、ResourcePackage owner、普通锁和 POSIX 路径不变。
- **Steering sync**：继承 Governance owner 的 ADR-027 及 ADR-020/023 取代元数据，不重复修改 Steering。
- **Downstream revalidation**：Task 3.4b 依赖平台 Task 3.3a，验证成功终结、journal 未闭合、原 recovery-required、收尾异常和真实跨进程互斥。Qt 不取得锁或 Win32 authority。

## 模块

### `tmx_context_contracts.py`

定义 frozen enums/DTO：`TmxScopeKind`、`TmxCarrierKind`、`TmxEffectiveLocales`、scope bindings、ordered `TmxExportUnit`、loss counts/issues、preview、single-use private plan、direct receipt/error。合同不 import Qt、Store、Workspace、Chunk 或 ResourcePackage 实现。

### `tmx_context_interchange.py`

拥有 `localcat-tmx-level1-context-v1`：

- canonical unit → deterministic TMX bytes；
- approved prop registry 与 unknown prop round-trip；
- inclusion/loss policy；
- 调 Parser application surface 做 staged/cold validation；
- 不自行查询 Store、Workspace、Chunk 或 package。

### `tmx_export_coordinator.py`

组合三类 owner port：

- Resource：capture/revalidate canonical export snapshot；
- Project：capture/revalidate `WorkspaceSessionView + WorkspaceUniverseProjection`；
- Chunk：再 capture/revalidate 一个 `ChunkScopeProjection`。

Coordinator 按 exact segment identity join，产生 ordered units 与 source binding。Preview 时捕获；apply 前重签并 exact compare。它不解析 TMX 或 ResourcePackage。

### `tmx_artifact_save.py` / `tmx_bound_artifact_save.py` / `tmx_platform_io.py`

`tmx_artifact_save.py` 保持 direct saver 的公开导入面；bound implementation 拥有 destination binding、persistent family lock、candidate/LKG/journal、atomic publish、readback 和 cold recovery，bounded I/O 只消费 platform rooted handles。普通持锁/恢复仍保留载体，只有下述 ADR-027 成功终结路径允许请求闲置回收。它只抛 TMX domain error，不返回 Resource/ProjectPackage receipt。

#### 输出锁终结条件

- `apply` 只有在 retained readback、receipt-ready journal durable commit、terminal reproof、owned LKG/stage/journal 清理和 retained publication 正常 close 全部完成后，才请求可选 `OutputArtifactLockRetirement.finish_output_lock(parent, lease)`；parent 在调用期间仍存活。原 direct receipt 内容和签发条件不变。
- `recover` 只有按既有冷恢复判定完成且本次 journal/sidecar 收尾完成后，才可请求同一能力；无 journal 的正常返回也可结束它本次已取得的输出 lease。若稳定 journal 仍存在或其 absence 无法证明，不请求回收。未知 journal、恢复结果不明或任何 `TMX.RECOVERY_REQUIRED` 路径只普通 close，保留载体和恢复证据。
- 本次 owner 终结判据在普通 lease 仍持有时完成；平台独占重开只重新认证当前控制载体，不证明全局没有另一进程的 recovery。现有稳定 journal 的检查归 TMX，不引入通用 recovery guard、跨进程 receipt 索引或新 journal schema。
- `finish_output_lock` 消费 lease close ownership；没有该能力时普通 close。`IN_USE`/`NOT_PROVEN` 不推翻已闭合 TMX receipt，也不得被映射成新的 publication/recovery failure；原发布/恢复异常必须保留。`RETIRED`/`ABSENT` 不授予 TMX 清理其他文件的权力。
- **文件边界与验证**：`tmx_bound_artifact_save.py` 接入终结能力；新增 `tests/test_tmx_output_lock_retirement_windows.py` 覆盖 resource/project/chunk direct 成功只留最终 TMX、冷 reader/receipt 不变、journal/sidecar 未闭合与原 recovery-required 不回收、收尾故障不推翻成功、正常冷恢复和 holder/waiter 竞争。不改变 `tmx_context_contracts.py`、scope adapters 或 ResourcePackage handler。对应 8.8–8.9。

### Parser / Import adaptation

`parser_tmx_codec.py` 在 TU/TUV scope 捕获 ordered `<prop>` metadata；保持安全 reader 和 limits。`resource_importer.py` 通过 registry 将 LocalCAT context/status/speaker/file/provenance props 转成 canonical `TMRecordDraft`，未知 prop 进入 provenance/format metadata 的无损表示；缺少 context 合法。

### ResourcePackage payload handler

LRP 的 capability matrix 扩为 kind × profile × carrier：

```text
translation_memory × localcat-tm-jsonl-v1 × direct|resource-package-v1
termbase          × localcat-termbase-csv-v1 × direct|resource-package-v1
translation_memory × localcat-tmx-level1-context-v1 × direct|resource-package-v2
```

后继 triple 使用 exact `localcat-resource-package-manifest-v2`、`localcat-resource-package-zip-v2`、`localcat-resource-payload-set-v2`，member 为 `payload/resource.tmx`，limit profile 独立版本化。LRP 通过注入 handler 获取/验证 payload；TMX 不拥有 manifest、carrier 或 transaction。首轮 profile 为 export-only，LRP 的 import/apply capability matrix 继续只接受 JSONL/CSV。

## Scope materialization

### Managed resource

完整 snapshot 按 canonical record order 转为 units。Binding 至少包含 resource id、store identity、generation、revision、record count、snapshot digest。

### Entire project

按 Workspace navigation order 遍历 session view，以 stable identity join universe presence。只接收 attached；project/document/current row/search 不改变范围。Binding 包含 project/session/workspace/composition revision、workspace/content/universe digest。

### Selected chunk

先取得明选 chunk projection，再按 exact membership 过滤 Workspace ordered units。Attached 进入 inclusion，detached 进入 loss count；missing/foreign/stale 阻断。Binding 叠加 plan/chunk/revision/digest。

## TMX mapping

- Header：creationtool=`LocalCAT`，creationtoolversion=`1`，segtype=`sentence`，o-tmf=`LocalCAT`，adminlang=`en`，srclang=effective source locale，datatype=`PlainText`。
- TU identity：scope kind + stable source identity 的确定性摘要，不使用显示名或路径。
- TUV：exact source/target locale；`<seg>` 仅文本。
- LocalCAT props：speaker、context-prev、context-next、file-source、confirmed/status、provenance key/value；未知 imported props 按原顺序追加并保留 duplicates。
- 复杂 inline XML 首轮不写；无法无损表示时 blocking loss。

## Qt

项目菜单新增无省略号的“导出项目”。Dialog 顶部用 `PROJECT` / `CHUNK` badge 和 scope selector；主体显示 ID/binding、文档/段落/损失统计、effective locale、profile、destination；底部仅“导出”“取消”。

资源页 `⋮` 独立新增“导出 TMX”，并保留“导出资源包”作为 carrier 选择。所有菜单标签不以省略号表达 dialog。Qt 只发 Controller command；preview 和 publish 在 worker 中运行。

## Failure semantics

- Scope stale/foreign/missing、effective locale invalid、blocking loss：candidate 前拒绝。
- Candidate/write/cold validate/fsync 失败：destination exact 不变。
- Replace/readback 失败：仅在 candidate identity 可证时恢复 LKG；否则 recovery-required。
- ResourcePackage handler 失败：由 LRP package transaction 返回失败；TMX 不签 resource receipt。
- 所有 public report body-safe，不回显翻译正文、context/provenance 值或绝对内部路径。
