# Collaborative Job Chunks 分簇评审协议

## 目的

本协议在既有 C0–C4 Promotion Cluster 内建立内部正交 review cells。它不新增实现 Cluster、不改变 Tasks 顺序，也不记录日期或评审经过；它只负责把复杂实施拆成互不代答的监督视角。有长期信息增量的语义修正必须回写 Requirements / Design / Tasks，实施状态由对应任务和可重放验证证据承载。

每个 Cluster 仍形成一个累计验证里程碑。review cells 只把不同 authority、故障面与验收问题交给定点检查；定点检查可以并行只读执行，但不能以多个局部检查替代最终累计 diff 验证。

## Promotion Cluster 评审边界

| Cluster | 监督问题 | 前置与输入 | 必须证明的证据 | 明确不审成什么 |
|---|---|---|---|---|
| C0 治理 | Chunk 是否只拥有协作视图，隐藏设计选择、上游依赖与后置 owner 是否已经冻结？ | Multi-Document 完整 C2；Chunk R/D/T/Research；ADR-018/019；相邻 Resource/TMX briefs | R/D/T traceability、v1 固定语义、limits、边界/import inventory 与 `spec.json` 一致性 | 不修改 runtime、tests、Controller、Qt、Steering 或 current-source evidence；不把后置 payload/carrier 当成 Chunk 能力 |
| C1 Identity / Topology / Store | exact、不重叠的 topology 能否独立持久、故障恢复并冷重开，同时不取得 assignment、ProjectPackage 或 export authority？ | C0 规格与边界基线；正式双 Document ProjectPackage export/open contract | 复合身份、rename/reorder/reopen、topology property/fault、全阶段 `assignee=None`、metadata cold recovery、ProjectPackage bytes 不变、scope forbidden-field scan | 不实现 assignment/permission/workspace reconciliation、Controller/Qt、TMX 或 ResourcePackage |
| C2 Assignment / Permission / Workspace Reconciliation | 谁可修改 target/confirmed，以及 workspace 变化后 membership 如何保持诚实而不变成账号或 reconciliation owner？ | C1 cumulative evidence；已发布 workspace/reconciliation facts | actor/manager/edit capability 矩阵、真实 mutation port 拒绝、stale capability、progress/detached、完整 workspace reconciliation fault matrix | 不实现 Controller session/search、Qt、provider/sync 或 export |
| C3 Controller / Search / Conflict | Application 是否只组合既有 authority，且 search、conflict、undo 没有形成第二 membership/store 权威？ | C2 cumulative evidence；Multi-Document C3 稳定 API | target/confirmed 实际调用链、`current_chunk` matcher/composite hit、旧 scope 不变、diverged 禁止 LWW、current-head undo 新 revision | 不实现 Qt 或 export transaction；search hit 不是 export scope |
| C4 Qt / Acceptance | UI 是否只是可信投影，并由同一 final tree 的真实命令与冷重开证据证明？ | C3 cumulative evidence；Multi-Document C4 稳定产品面 | 真实 ProjectPackage + metadata journey、Qt disabled 与 Controller denial 双证、窄布局/键盘、无-plan legacy、strict evidence reread | 不新增领域语义、不直接读 store、不增加 TMX/ResourcePackage/project export UI |

## 正交 review cells

### C1A — Contracts / Identity / Topology

- 审查 `ChunkSegmentRef`、plan/chunk ID、canonical member order、universe/plan digest 与 `ChunkLimitProfile v1`。
- 审查不重叠 partial partition、Unallocated、create/rename/reorder/split/merge/move/dissolve 的 exact union/disjoint 与 retired ID。
- 审查 owner-issued scope issue/revalidate seam、完整 membership 和 forbidden-field closure。
- 审查 topology-only manager capability 的 project/session/plan/operation/single-use binding。
- 逐阶段证明 C1 active chunks 的 `assignee=None` 和 assignment counts `0`；任何 assignment intent 在 private candidate 前零 mutation 拒绝。

### C1B — Metadata / Publication / Recovery

- 审查 strict canonical JSON、duplicate/extra/type/depth/byte/count limits 与 cold decoder。
- 审查 candidate → cold validate → revalidate → journal/LKG → replace/durability → cold readback → cleanup proof。
- 审查首次 LKG `None`、覆盖保留 exact LKG、不确定状态 recovery-required 与 audit chain。
- 证明 metadata-only 操作不改变 ProjectPackage、workspace、正文、dirty 或权限。

### C2A — Actor / Assignment / Permission

- 审查 identity owner port、opaque assignee、local/reference 诚实边界与 credential absence。
- 审查 manager、assignment 与 edit capabilities 不互相扩权；旧 capability 在任何 binding 变化后失效。
- 用实际 target/confirmed mutation port 证明 assigned + current + attached 是唯一可写组合。

### C2B — Progress / Workspace Reconciliation

- 审查 progress 只从 current workspace 与 exact membership 派生，detached 不进分母。
- 审查 unchanged/source_changed/detached/missing/new 的完整矩阵，不按 index/text/display 猜测。
- 审查 missing 的逐项显式处置和 new Unallocated。

### C3A — Controller / Session / Search

- 审查 Controller 只消费 frozen service/projection/capability，不解码 metadata 或 ProjectPackage。
- 审查每个 target/confirmed mutation 在首次 workspace mutation 前重验全部 binding。
- 审查 `current_document`、`entire_project` 与 `current_chunk` 正交，且复用同一 matcher 与 composite hit。

### C3B — Conflict / Audit / Undo

- 审查 identical/fast-forward/stale/diverged/foreign/universe-mismatch 的 closed semantics。
- 审查禁止 LWW/自动 union；冲突未解决时 plan/workspace 保持不变。
- 审查 current-head-only undo 恢复 exact previous snapshot，并以新 revision/receipt 发布。

### C4P — Qt Product Surface

- 审查 Document 与 Chunk 控件、名称和导航不混同。
- 审查首页不存在 Chunk 控件，Project 下拉是管理/选择分工入口，且选定分工后编辑、浏览/校对与 Document 文件夹使用同一 exact membership 投影；高级选段由 manager 签发一次性 exact-identity 请求并复用浏览/校对全项目双语表，完成/取消恢复 mode/document/segment/search/current chunk，浏览页不获得 topology preview/apply authority。
- 审查“拆分项目 / 拆分分工 / 合并分工”主路径可直接完成动态 2–N 原子 publication，assigned child/result 决策显式且高级操作不挤占主路径。
- 审查 preview/apply、只读 reason、Unallocated/detached/progress 与 local/reference 标签的可访问反馈。
- 审查 Qt 不直接访问 store、metadata JSON、provider、ResourcePackage 或 TMX export。

### C4E — Final Evidence

- 在同一 final tree 上复验 architecture、contract/property、fault、compatibility 与真实冷重开 journey。
- 证明无 active plan 的 legacy/ProjectPackage 行为不变，所有阻断故障保留 LKG/workspace/dirty/navigation/permission。
- 复验 scope projection 不泄漏正文、路径、assignee、payload、carrier、destination、loss 或 receipt。

## 闭合规则

1. 每个 review cell 必须以实际累计 diff 和 fresh mechanical evidence 为输入；设计陈述或历史 PASS 不能替代。
2. 任一 cell 的 Critical/Important finding 未关闭时，对应 Promotion Cluster 保持未完成。
3. 全部定点检查关闭后，仍须复核完整 Cluster 累计 diff、跨 cell interaction 与边界。
4. 只有最终累计实施验证通过，才能勾选该 Cluster tasks 并进入下一簇；C4 闭合后再形成 Chunk 特性提交。
5. 实现与评审可以分工；局部测试不能替代独立边界检查。
