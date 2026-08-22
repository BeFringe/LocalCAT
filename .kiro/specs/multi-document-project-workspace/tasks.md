# 实施计划

## 任务说明

本计划把 brief 的四个 Promotion Cluster 保持为 Cluster 1–4；Cluster 0 只完成治理、current-source characterization 与人工批准门。Cluster 0 未获项目 owner 明确批准前，业务代码一律 **NO-GO**。

固定顺序：

```text
Cluster 0 R/D/T + ADR + characterization + 人工批准
  → Cluster 1 身份与 origin
  → Cluster 2A 聚合/reconciliation
  → Cluster 2B save/recovery
  → Cluster 2C ProjectPackage logical + physical 闭环
  → Cluster 3 应用服务
  → Cluster 4 Qt 与 current-source acceptance
```

每个 Cluster 只形成一次阶段性 Git 提交，并由独立 reviewer 对累计 diff 做对抗审查。Cluster 2 的 2A/2B/2C 是同一 Promotion Cluster 内的顺序门；三段都闭合后才形成 Cluster 2 提交。不得把下一 Cluster 的 production 变化抢跑进当前提交。

## Cluster 0：治理、Characterization 与人工批准

- [x] 0.1 完成中文 Requirements 与 Research
  - 冻结 `Project → Document → Segment`、三类 origin、复合身份、保存/reconciliation、ProjectPackage 与 UI 边界。
  - 记录当前单 JSON/TXT project、Parser/Codec、Controller/Search 与 Qt 行为基线及已知兼容入口。
  - 明确本规格不拥有 TM/术语 ResourcePackage、TMX、RPY 语法、PO/POT writer、CONTEXT、chunk 或 sync provider。

- [x] 0.2 完成中文 Design 并复核已采纳 ADR-018
  - 设计 immutable contracts、聚合边界、稳定身份、dirty/save/reconciliation、逻辑 manifest、package 事务、Controller 与 Qt 投影。
  - ADR-018 记录 ProjectPackage authority、`codec_private_member` 不透明边界与 package/import/apply 事务；Cluster 0 必须证明 R/D/T/border 与该已采纳决策一致。
  - 不在 Cluster 0 决定 ProjectPackage 是目录、单文件 archive 或其他物理 carrier；该决策留到 2C 实现前的人工门。

- [x] 0.3 冻结 Tasks、review clustering、border 与所有权
  - 保持 brief 的 Cluster 1–4 编号和依赖；Cluster 2 明确拆为 2A/2B/2C。
  - 每 Cluster 一次提交、一次独立对抗 review；失败时只做窄 remediation，不重写已审计的前序 Cluster。
  - `feature/multi-document-project-workspace` 只写 owning Spec、经批准的 ADR-018/review/border 与精确 Steering 同步。

- [x] 0.4 建立 current-source characterization
  - 枚举 `editor_contracts.py`、`editor_project.py`、`editor_controller.py`、`project_search.py`、`workspace_state.py`、`parser_contracts.py`、`parser_composition.py`、`parser_source.py`、`qt_editor.py`、`qt_editor_window.py` 的 import/constructor/call/patch/serialization consumer。
  - 冻结单 JSON/TXT 打开、编辑、搜索、dirty、保存、重开、故障和 Qt session 的 current-source 行为；记录真实 fixture 与 deterministic digest/inventory。
  - 建立负向 architecture guards：Parser/Codec 不聚合 workspace，workspace 不解释 `codec_private_member`，chunk/sync/resource/TM authority 不反向进入。

- [x] 0.5 取得 Cluster 0 人工批准
  - Requirements、Design、Tasks、ADR-018、review clustering、border 与 characterization 全部由独立 reviewer 审查。
  - 项目 owner 明确批准后才可勾选本项并开始 Cluster 1；无批准即保持业务代码 NO-GO。

### Cluster 0 完成门

- 0.1–0.5 全部完成且人工批准记录可追踪。
- production、产品 UI 与 current-source evidence payload 零变化。
- 独立提交：`docs(multi-document): 冻结 workspace 治理与基线`。

## Cluster 1：身份与 Origin（brief Promotion Cluster 1）

- [x] 1.1 建立 Project / Document / Segment immutable contracts
  - `ProjectDocument` 使用稳定 `document_id`；项目内 segment identity 固定为 `(document_id, local_segment_id)`。
  - display name、sheet name、列表位置和枚举顺序不得成为持久身份。

- [x] 1.2 建立 `single_file` / `directory` / `workbook` origin 与稳定 `source_ref`
  - 规范化相对 `source_ref` 与 manifest-issued ID 的优先级、路径安全和重复/冲突拒绝语义。
  - origin 只描述项目来源，不据此宣称任意 XLSX、RPY、PO/POT 或其他 codec 已可写。

- [x] 1.3 建立既有单 JSON 兼容适配
  - 既有单 JSON 行为可投影为一个 Project/一个 Document，但不得改变其 current-source open/save/segment identity 语义。
  - Workspace v1 eligibility 在 adapter 提升时执行；不合规旧文件结构化拒绝且原字节不变，legacy `load_project()` / `save_project()` 仍完全兼容。
  - 当前单 JSON 路径继续可独立工作，不把本规格变成 Parser Foundation 的反向前置。

- [x] 1.4 闭合身份与 origin 的对抗测试
  - 覆盖重命名、重排、同名文档、路径规范化、重复 ID、伪造复合 ID、跨项目身份与旧入口兼容。
  - 独立 reviewer 证明没有临时路径/显示名/列表索引权威。

### Cluster 1 完成门

- immutable identity 与三类 origin 通过 current-source compatibility 和 hostile fixtures。
- 独立提交：`feat(workspace): 建立多文档身份与 origin`。
- Cluster 1 独立对抗 review 通过后才进入 Cluster 2A。

## Cluster 2：聚合、持久化与手工 ProjectPackage 闭环（brief Promotion Cluster 2）

### Cluster 2A：Aggregation / Reconciliation

- [x] 2.1 建立 document ordering 与连续 workspace 聚合
  - manifest/import 顺序只决定显示和导航顺序，不改变 Document/Segment 身份。
  - workspace 组合 codec 已产生的单文档结果，不复制格式 grammar 或 writer。
  - 提供有界显式文件 intake：用户选择同一 portable root 下的 JSON/TXT/PO/POT 列表，逐个取得 Parser verified terminal 后才聚合；不扫描目录、不自动包含相邻文件、不赋予 reader-only writer。
  - 保留同一 root fd 和所有 selected regular-file identities 至整批 terminal 完成，拒绝 hardlink/symlink/root replacement/file drift；发布的 staged DTO 必须明确 `durable=False` 且原 source bytes不变。
  - 冻结 flat segment 投影、document/project progress 与 deterministic workspace content digest；现有 Document 不得因 incoming selection reorder 改变顺序，真正 new Document 只按 incoming 顺序追加。

- [x] 2.2 建立 source reconciliation
  - 按稳定复合 ID 与 source fingerprint 产生 `unchanged`、`source_changed`、`new`、`removed`、`ambiguous`、`unresolved`。
  - `source_changed` 保留 target 但撤销确认；`removed`、`ambiguous`、`unresolved` 保留恢复引用并要求显式处置，不按列表索引或正文相似猜测。
  - 冻结设备本地 `OriginBinding` 的 exact root/source_ref/revision → document_id 回接；新 source identity 允许变化并参与 reconciliation，preview 后再变才 stale。重命名只经显式映射，forged/stale/cross-root binding 在首次 mutation 前拒绝。
  - `keep_detached` 后 detached-only Document 留在 workspace，但不伪造 live `OriginBinding`；apply 仅消费当前 service 签发的一次性 operation，必须复验 project/session/revision/workspace/source identities 后才单次 swap。

### Cluster 2B：Save / Recovery

- [x] 2.3 建立 carrier-neutral save candidate、LKG 与结构化报告
  - 冻结逐 Document baseline、完整 candidate、staging/validation/publication/readback 与 last-known-good 语义；只清除已证明持久化的 dirty。
  - 报告区分 `saved`、`rolled_back`、`unchanged`、`failed` 与不确定/需恢复状态，不用项目级布尔值抹平局部结果。

- [x] 2.4 闭合 save/recovery fault model 与未来 origin 原子性红线
  - 覆盖 candidate、validation、publication、readback、commit、rollback 与 cold recovery fault；任何不确定状态保持 LKG、dirty 与恢复信息。
  - directory/workbook 仅冻结后续 profile 必须遵守的逐文档 journal/单文件原子替换红线；本 Cluster 不启用 directory discovery、workbook project profile 或新 source writer。

### Cluster 2C：ProjectPackage Logical + Physical Closure

- [x] 2.5 建立版本化 `ProjectPackageManifest` 与 carrier-neutral package contracts
  - manifest 固定项目/文档身份、顺序、member reference、版本与 digest；未知版本、重复 member、digest/identity/path 冲突 fail closed。
  - document content 与 `codec_private_member` 只作为带 digest 的 member；通用 workspace 不解释 codec-private bytes。
  - preview 无写入且与最终 apply 消费同一已验证计划；receipt 对账 package identity/version/member digest、reconciliation 与逐文档结果。
  - 不把 TM/术语资源装入 ProjectPackage，也不建立 ProjectPackage/ResourcePackage 共同 authority。

- [x] 2.6 在任何 2C production 实现前批准 ProjectPackage 物理 carrier 决策
  - 用 current-source prototype/fixture 比较目录、单文件 archive 或其他候选的确定性、原子替换、路径安全、流式校验与恢复语义。
  - 把选定 carrier、版本迁移和拒绝方案写入 owner 批准的 C2C decision record；若治理门判定需新 ADR，则新增后继 ADR，不改写已采纳 ADR-018；未批准时 2C implementation NO-GO。
  - 人工已批准 ADR-019：v1 唯一 carrier 为严格闭集的 `localcat-project-package-zip-v1`/`ZIP_STORED`；拒绝 ZIP64、压缩、宽松 `zipfile` 读取和并行 directory reader/writer。

- [x] 2.7 实现手工 export / validate / preview / import / apply / receipt
  - export 只在完整 staging、member digest 和 readback validation 成功后发布，不完整导出不得覆盖旧包。
  - import 复验物理 carrier 与逻辑 manifest，并让 preview 后的 apply 使用同一事务/计划；失败保留旧项目与可重试信息。

- [x] 2.8 闭合冷重开与 package fault matrix
  - 覆盖截断、重复/缺失/额外 member、路径穿越、digest/version/codec声明不符、preview 后篡改、commit/readback/reopen 失败。
  - live codec unavailable 只产生 body-safe warning并禁止source write-back，不阻止package离线导入/target编辑；只有声明损坏或请求解释private member的操作才fail closed。
  - 用至少两个 Document 且跨文档复用同一 local segment ID 的真实 ProjectPackage 冷重开，逐项核对项目/文档/segment 身份、顺序、source/target、opaque member 与 receipt。

### Cluster 2 完成门

- 2A/2B/2C 各自通过定点对抗检查，累计 diff 再通过 Cluster 2 独立 reviewer。
- `collaborative-job-chunks` 的统一进入门位于完整 Cluster 2 之后；不得在仅有 Cluster 1 identity 时开始 chunk schema/权限实现。
- Cluster 2 后恢复/确认 `language-resource-portability` brief，再提升 TM JSONL 与术语 CSV/v1 ResourcePackage R/D/T；`tmx-context-interchange` 未来只拥有可选 TMX export profile。两项不得互相冒充或抽取 ProjectPackage 共同 authority。
- 独立提交：`feat(workspace): 闭合 ProjectPackage 手工包事务`。

## Cluster 3：应用服务（brief Promotion Cluster 3）

- [ ] 3.1 建立 Controller workspace session 与 issued identity
  - open/switch/edit/save/reconcile/package 操作只接受当前 session/generation 签发的 Project/Document/Segment 身份。
  - stale/forged/cross-project identity 在任何 mutation 前 fail closed。

- [ ] 3.2 建立项目/文档 dirty 与保存状态投影
  - 文档状态聚合为项目状态但不抹平逐文档失败；保存成功只清除已证明持久化的 dirty。
  - package import/apply 与 source reconciliation 使用 Cluster 2 冻结的事务和 receipt。

- [ ] 3.3 建立可扩展 search scope
  - 首批只开放 `current_document` / `entire_project`，UI 文案为“当前章节 / 搜索全部章节”。
  - 可保留未来 `current_chunk` enum 扩展位，但不得映射成 Document、查询未实现 chunk 或暴露 chunk 控件。

- [ ] 3.4 闭合应用层并发、故障与兼容矩阵
  - 覆盖切换章节、session 替换、reconcile/save 竞态、stale search result、部分保存与旧单 JSON controller journeys。
  - 独立 reviewer 证明 Controller 不解释 codec grammar/private member，也不拥有 provider/chunk/TM authority。

### Cluster 3 完成门

- application 只消费 Cluster 1–2 已批准 contracts；失败不丢失 dirty、identity 或恢复信息。
- 独立提交：`feat(workspace): 接入多文档应用会话`。
- Cluster 3 独立对抗 review 通过后才进入 Cluster 4。

## Cluster 4：Qt 与 Current-source Acceptance（brief Promotion Cluster 4）

- [ ] 4.1 实现章节导航与连续段落体验
  - 显示章节名/当前位置，支持跳到章节首段；重命名/重排只改变投影，不改变持久身份。
  - 编辑/浏览模式均保留明确章节分隔；窄宽布局不隐藏当前章节或保存失败状态。
  - “新建多文档项目”只消费 Cluster 2A 的显式选择入口，展示选中文档和顺序；不在 Qt 复制 suffix/Parser grammar 或递归目录发现。

- [ ] 4.2 接入搜索范围、dirty 与保存/recovery 反馈
  - UI 只通过 Controller 消费 `current_document` / `entire_project`、逐文档保存报告和 reconciliation/receipt。
  - 不在 Qt 复制 manifest、codec、reconciliation、package 或 identity authority。

- [ ] 4.3 使用真实 ProjectPackage substrate 完成 current-source acceptance
  - acceptance 从 Cluster 2 正式 exporter 生成真实 ProjectPackage，经 validate/preview/import/apply 后冷重开，再运行章节导航、编辑、搜索、保存与恢复 journeys。
  - 不得用内存伪 package、手写 manifest 或 fixture-only controller 注入代替真实 substrate。

- [ ] 4.4 重签 current-source evidence 并完成治理收尾
  - 在 final runtime roots 冻结后运行全量 identity/reconciliation/save/package/controller/Qt/fault/acceptance 与单 JSON/TXT compatibility suites。
  - owner 工具生成并由 strict consumer 复读 evidence；随后只允许不属于 source roots 的 Tasks/Steering/border completion 更新。
  - 同步真实结构/技术事实并由 Cluster 4 独立 reviewer 给出 Feature GO/NO-GO。

### Cluster 4 完成门

- 至少两个 Document 的真实 ProjectPackage 冷重开与 Qt current-source journeys 全部通过；directory/workbook 产品 profile 继续保持未启用的负向边界。
- evidence 绑定同一 final runtime tree；任一 package、identity、保存或兼容失败均为 NO-GO。
- 独立提交：`test(workspace): 重签多文档 current-source 证据`。

## 相邻规格边界

- 恢复/确认 `language-resource-portability` brief 后，提升独立 R/D/T，拥有 TM JSONL 与术语 CSV/v1 ResourcePackage、报告和冷重开；sync 分别消费已批准 ProjectPackage/ResourcePackage，不复制 live SQLite、journal、sidecar 或 staging residue。
- `tmx-context-interchange` 只拥有 ResourcePackage 未来可增加的 TMX export profile、TMX context/provenance 与有损取舍；TMX 不是 ProjectDocument，也不由本规格开放 TMX writer。
- `rpy-project-codec` 独立拥有 RPY token/sidecar/占位符与 writer；folder 接入依赖本规格，但产品排期仍在 sync 后。
- PO/POT reader 或未来 canonical writer 归独立 format codec；本规格的 `single_file`/`directory` origin 不自动赋予 PO/POT writer 能力。
- CONTEXT 精确语义及“上下文一致”UI 投影归 `feature5-ui-integration`（Integration TM surface）；本规格不增加 evidence 字段或匹配判定。
- `collaborative-job-chunks` 在完整 Cluster 2 后才可开始，只引用稳定 segment membership，不拥有文档身份、项目保存或远程传输。

## 明确禁止

- Cluster 0 人工批准前修改任何 production/runtime/UI/evidence payload。
- 用显示名、sheet 名、文件枚举顺序、列表索引或临时绝对路径充当稳定身份。
- 让通用 workspace、chunk、sync provider 或 Qt 解释 `codec_private_member`。
- 在 2C carrier 决策获批前实现或暗定 archive/directory 物理形态。
- 把 ResourcePackage 与 ProjectPackage 抽象成共同 authority，或让 sync 直接复制 live canonical store。
- 抢跑 TMX export、RPY product rollout、PO/POT writer、CONTEXT UI、chunk 权限或 remote provider。
