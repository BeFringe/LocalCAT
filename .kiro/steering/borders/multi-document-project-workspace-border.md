# 设计约束清单 — Multi-Document Project Workspace

## 设计来源

- 项目 owner 批准从 Multi-Document Cluster 0 回归，先完成 R/D/T、ADR、current-source characterization 与人工批准；业务代码暂为 NO-GO。
- `multi-document-project-workspace/brief.md` 及待批准 Requirements/Design/Tasks。
- Parser/Codec、Qt increment、TM Store maintenance 已完成的相邻边界事实。
- ADR-015 与已采纳 ADR-018。

## Critical Path

```text
Cluster 0 人工批准
  → Cluster 1 identity/origin
  → Cluster 2A aggregation/reconciliation
  → Cluster 2B save/recovery
  → Cluster 2C carrier批准→ProjectPackage logical+physical事务
  → Cluster 3 Controller
  → Cluster 4 Qt + current-source evidence
```

隐性依赖：Cluster 3 的 issued identity/dirty/search scope 依赖 Cluster 1 的稳定身份和 Cluster 2 的真实保存/reconciliation/receipt；Cluster 4 的 UI acceptance 必须消费 Cluster 2 正式 exporter/importer 产生的真实 ProjectPackage，不能用 fixture-only substitute。Chunk 的统一进入门是完整 Cluster 2，而不是仅有 Cluster 1 identity。

## 红线

| 约束 | 验证锚点 |
|---|---|
| Cluster 0 人工批准前业务代码 NO-GO | production/runtime/UI/evidence payload 零 diff；0.5 仍未批准时不得进入 Cluster 1 |
| 稳定复合身份不依赖展示或枚举 | rename/reorder/same-name/reopen/forgery fixtures；`(document_id, local_segment_id)` exact equality |
| Parser/Codec 与 workspace 聚合分权 | Parser 只产单文档；workspace 不复制 grammar/writer；architecture closed-world |
| 首个多文档入口不偷渡目录扫描 | 显式 JSON/TXT/PO/POT 选择列表；每文件 verified terminal；未知/未选文件零消费 |
| `codec_private_member` 始终 opaque | workspace/Controller/Qt/chunk/sync 不解析、不改写、不提升为第二权威 |
| save/recovery 与未来 origin 原子性诚实 | carrier-neutral candidate/LKG/report 闭合；directory/workbook 只冻结后续 profile 红线，不启用产品入口；failure 保留 dirty |
| 2C 实现前批准物理 carrier | owner 决策记录先于任何 archive/directory carrier production diff |
| ProjectPackage preview/apply 同一事务 | digest/version/path/identity 重验；preview 后 tamper 拒绝；失败保留旧项目/旧包 |
| Cluster 4 使用真实 ProjectPackage | 正式 export→validate→preview→import/apply→cold reopen 完整 journey |
| 每 Cluster 一提交与独立对抗 review | base/tip、累计 diff、review verdict 和 remediation 可追踪 |
| Chunk 统一后置完整 Cluster 2 | Cluster 2 未完成前，chunk schema/权限/runtime/UI 零 diff |
| ProjectPackage 不吞并 ResourcePackage | TM JSONL/术语 CSV/v1 留给独立 `language-resource-portability` brief，无共同 authority |
| 相邻格式/匹配权威不越界 | TMX/RPY/PO-POT/CONTEXT negative architecture 与 scope tests |

## 灰线

- Cluster 1 的单 JSON adapter 不改旧入口、对象行为、保存和冷重开；提升为 Workspace 时单独执行 v1 eligibility。不合规旧值必须 body-safe 拒绝提升且原文件不变，不得截断或重铸 ID；legacy facade 仍 exact compatible。
- Cluster 2B 可用 carrier-neutral fault harness 验证 candidate/LKG/save/recovery；Cluster 2C 可用 carrier-neutral fixture 先验证逻辑 manifest，但两者都不能替代 2C/Cluster 4 的真实物理 ProjectPackage 验收。
- ProjectPackage 物理 carrier 可在获批决策中选择目录、单文件 archive 或其他等价方案；本 border 不预先指定实现。
- 内部 search scope 可为未来 `current_chunk` 保留扩展位，但当前不得访问 chunk owner、映射 Document 或显示控件。

## 相邻规格边界

- `language-resource-portability`：现有 brief 在 Cluster 2 package 原语被验证后提升为独立 R/D/T，独立拥有 TM JSONL 与术语 CSV/v1 ResourcePackage、报告、preview/receipt 和冷重开；sync 分别消费 ProjectPackage 与 ResourcePackage。
- `tmx-context-interchange`：只拥有 TMX language-resource context/provenance/export profile；TMX 不是 ProjectDocument。
- `rpy-project-codec`：独立拥有 RPY tokenization、sidecar、placeholder 与 writer；多文件 folder 聚合才消费 workspace，产品排期仍在 sync 后。
- PO/POT canonical writer：未来独立 format-codec 决策；本规格不因 readable origin 宣称可写。
- `feature5-ui-integration`（Integration TM surface）：CONTEXT 精确判断与“上下文一致”标签；本规格不增加 evidence 字段或重算匹配。
- `collaborative-job-chunks`：完整 Cluster 2 后只引用稳定 segment membership，不拥有 Document identity、项目保存或远程 provider。

## 降级决策记录

> 未经项目 owner 明确批准，不允许跳过 Cluster 0/2C 人工门、降低真实 ProjectPackage 验收、提前 chunk，或把相邻规格能力并入本规格。

| 日期 | 降级 | 原因 | owner批准 |
|---|---|---|---|
| （空） | | | |

## 当前状态与归档门

> 当前状态：Cluster 0 已取得项目 owner 人工批准；Cluster 1 identity/origin 实施 GO。

- [x] Requirements / Design / Tasks / ADR-018 / review clustering / border 一致并获人工批准
- [x] current-source characterization fresh 通过，Cluster 0 production 零变化
- [ ] Cluster 1 identity/origin 与单 JSON 兼容通过
- [ ] Cluster 2A 聚合调和、2B 保存恢复、2C carrier 决策与真实 ProjectPackage 冷重开通过
- [ ] Cluster 3 Controller session/dirty/search scope 通过
- [ ] Cluster 4 Qt、fault、acceptance 与 current-source evidence 通过
- [ ] 每 Cluster 独立提交与独立对抗 review 可追踪
- [ ] Chunk/Sync/ResourcePackage/TMX/RPY/PO writer/CONTEXT 等相邻线无越界
