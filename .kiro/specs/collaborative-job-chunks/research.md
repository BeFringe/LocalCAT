# 研究与边界决策

## Summary

- **Feature**：`collaborative-job-chunks`
- **Discovery Scope**：在已闭合的 Multi-Document C2 ProjectWorkspace/ProjectPackage 之上建立独立协作分工语义，不接管项目持久、source reconciliation 或远端传输。
- **Key Findings**：
  - 当前上游已冻结 `SegmentIdentity(document_id, local_segment_id)`、stable `project_id`、workspace revision、source presence、ProjectPackage content/artifact digest 与 reconciliation receipt，足以支撑 exact membership；Chunk 无需解析 manifest 或复制 source/target。
  - ProjectPackage v1 的 logical/physical member 闭集已由 ADR-018/019 冻结，没有 extension 或 chunk member。Chunk 若直接增加字段，会越过已批准 schema/profile 并让 chunk 拥有 ProjectPackage save authority。
  - Chunk C1/C2 仅需 Multi-Document C2；`current_chunk` 的 search/Controller 结合依赖 Multi-Document C3，Qt 章节界面上的 chunk 产品投影依赖 Multi-Document C4。这两个隐性依赖必须是独立门，不能通过在 chunk 中复制 Controller/Qt 解决。
  - C1 可先冻结 optional `assignee` 字段 shape 以保持 metadata 合同单一，但 C2 才独占激活 non-null assignment 与选择语义。因此 C1 对 create/decode/preview/persist/cold-reopen 全链路强制 `assignee=None` 与 assignment counts `0`。
  - 当前产品没有账号或身份认证服务。Chunk 可以冻结 opaque assignee 引用和授权判定，但不能自己创建账号、保存凭据或把 display label 当身份。

## Sources Consulted

- `.kiro/specs/collaborative-job-chunks/brief.md`
- `.kiro/specs/multi-document-project-workspace/{brief.md,requirements.md,design.md,tasks.md,research.md,spec.json}`
- `.kiro/steering/adr/adr-018.md`
- `.kiro/steering/adr/adr-019.md`
- `.kiro/steering/{spec-ownership.md,roadmap.md,structure.md,tech.md,product.md}`
- `project_workspace_contracts.py`、`project_workspace.py`、`project_save.py`、`project_package.py`、`project_search.py`、`editor_controller.py`

## Current Contract Inventory

### 可直接消费的上游事实

- `ProjectWorkspace` 拥有 exact `project_id`、有序 Documents 与每文档 exact local segment IDs。
- `SegmentIdentity` 已使用 `(document_id, local_segment_id)`，跨 Document 重复 local ID 合法。
- `ProjectSourceSegment.source_presence` 区分 attached/detached；workspace reconciliation 已拥有 unchanged/source_changed/new/removed/ambiguous/unresolved 语义。
- `ProjectWorkspaceService` 拥有单调 revision、flat view、Document/Project progress 和一次性 reconciliation preview/apply。
- ProjectPackage 已封闭 export/validate/preview/import/apply/receipt 和严格 deterministic ZIP v1；chunk 消费 cold-opened workspace，不解析物理 carrier。

### 不能被误当成上游能力的事实

- 在本次提升所依据的 Multi-Document C2 completion commit `65fadcf` 上，C3/C4 仍属后续任务，`project_search.py` 仍是单 `EditorProject` 的扁平搜索服务。Chunk C3 启动时必须重读届时已批准 Multi-Document C3 current-source contract，不能把本轮基线或并行开发中的暂态当成已有 `current_document`/`entire_project`/`current_chunk` 集成。
- ProjectPackage v1 不允许未知 member/schema field；本规格不能把实验性 chunk JSON 塞入 ZIP。
- 当前没有身份验证或 provider；本规格只能冻结接口与本地 owner 组合，不能声称已有多用户在线协作。

## Alternatives Considered

| Option | Strengths | Risks / Boundary break | Decision candidate |
|---|---|---|---|
| 把每个 Document 当一个 chunk | 实现简单 | 无法跨文档/离散分工，污染 Document identity | Reject |
| 持久起止下标或连续 range | payload 小 | reorder/reconciliation 后漂移，跨 Document 语义不稳定 | Reject |
| 存 exact Segment_Ref set，view 时按 workspace 顺序投影 | identity 稳定，支持连续/离散/跨文档 | 需要显式 rebase 缺失成员 | Select |
| active chunks 允许重叠 | 可支持多人同段编辑 | 编辑权限和进度重复，需 OT/CRDT/语义 merge | Reject for v1 |
| 不重叠部分划分 + Unallocated | 权限闭合，允许分阶段分工 | 重叠审校需后续规格 | Select candidate |
| chunk metadata 写入 ProjectPackage v1 | 手工搬运方便 | 违反 ADR-018/019 闭集与 owner 边界 | Reject |
| 独立 namespaced chunk snapshot | 不改项目包，可由未来 sync 当 opaque metadata 搬运 | 需另行批准 transport binding | Select candidate |
| 冲突自动 union/LWW | 看似无需人工 | 可扩大权限、丢失 assignment 或产生 overlap | Reject |
| exact revision + divergent preview | 权限安全，冲突可审计 | 需管理者显式处理 | Select candidate |

## Proposed Decisions Awaiting Human Approval

### 1. v1 是不重叠部分划分

Active chunks 的 attached members 对 attached Segment Universe 是不重叠的 partial partition，允许 Unallocated；rebase 后保留的 detached member 仍全局不重叠，但不进 attached partition。这使 target/confirmed 编辑权限有唯一归属，也避免在没有 OT/CRDT 时进入同段多写者。未来协作审校若需重叠 scope，应发布新 policy version，不宽松 v1。

### 2. v1 每 chunk 最多一个 assignee

该选择在 C2 激活：一个译者可同时拥有多个 chunks，但一个 chunk 不并发授权多个 target 写者。C1 只冻结 optional field shape 且只接受 `None`。这不是账号系统；assignee 只是 opaque authority/subject ref。

### 3. chunk metadata 与 ProjectPackage v1 物理分离

本规格批准独立 `localcat.collaboration.chunks.v1` logical snapshot 和本地原子持久，不更改 ProjectPackage v1。未来 sync 必须为“新 ProjectPackage namespaced extension”或“独立 companion object”单独选型；本规格不预批准任一种 transport。

### 4. detached 成员保留但只读，不计完成率

`keep_detached` 仍保留 exact segment identity 和恢复价值，因此 membership 不自动删除；但该 source 已脱离 active origin，该段在 chunk 中只读且单独计数。

### 5. 安全撤销仅恢复当前 audit head

v1 不重写历史或在后续变更上运行猜测性 inverse。撤销必须指向当前 head，复验 workspace/plan binding，然后以新单调 revision 发布 previous snapshot。

### 6. 当前只用诚实标签的 local/reference actor

当前没有 account/auth owner，v1 可以用明示为 local/reference 的 actor port 验收权限引擎，但不宣称它是安全账号认证、跨设备身份或 provider principal。Display label 不参与权限比较。

### 7. 一个 active Project session 只有一个 active Chunk Plan

v1 不同时发布多套竞争分工方案；备选方案只能在 private preview 中存在，显式 apply 后替换当前 plan revision。这使 target/confirmed 编辑权限始终只有一个可重验权威。

## ADR Disposition

本轮不起草新的项目级 ADR。上述设计局限在 `collaborative-job-chunks` 的领域合同。如果改为“写入 ProjectPackage v1”、“跨设备自动语义 merge”或“重叠多写者权限”，将改变 ADR-018/019 或跨规格 authority，必须先起草后继 ADR。
