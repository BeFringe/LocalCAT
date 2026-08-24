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

### `tmx_artifact_save.py`

direct `.tmx` 的 carrier-neutral destination binding、candidate/LKG、fsync、atomic replace、readback 和 recovery。它只抛 TMX domain error，不返回 Resource/ProjectPackage receipt。

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

- Header：creationtool=`LocalCAT`，segtype=`sentence`，adminlang=`en`，srclang=effective source locale，datatype=`PlainText`。
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
