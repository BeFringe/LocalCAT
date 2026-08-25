# 实施计划

## 任务说明

本计划保持 brief 的四个 Promotion Cluster，并在它们之前增加只做治理的 Cluster 0。`multi-document-project-workspace` 完整 C2A/C2B/C2C 提供真实 ProjectPackage 作为 Chunk C1 的上游输入。

`spec.json` 的 Requirements/Design/Tasks 三个 `approved` 和 `ready_for_implementation` 已置为 `true`。Cluster 1 只实现 identity/membership/topology/local metadata；不得提前引入 assignment、Controller/Qt、ResourcePackage 或 TMX 责任。

固定顺序：

```text
Multi-Document C2 complete
  → Chunk C0 R/D/T + boundary baseline
    → C1 identity/membership/topology/local metadata
      → C2 assignment/permission/progress/rebase
        → [Multi-Document C3 complete] C3 Controller/search/conflict/undo
          → [Multi-Document C4 complete] C4 Qt/current-source acceptance
```

每个 Cluster 形成独立的累计验证里程碑，并以当前累计 diff 的可重放验证和正交边界检查作为进入下一簇的实施证据；C0–C4 最终收束为一个 Chunk 特性提交。`review-clustering.md` 将 C1–C4 拆成互不代答的监督单元，用于检查 authority、故障面与验收覆盖，不改变 Requirements / Design / Tasks 的语义权威。后续 Cluster 不得抢跑未收束的前置边界，也不得为了压缩历史而把 ProjectPackage、provider、account 或 TM 责任并入 Chunk。

## Cluster 0：R/D/T 与边界基线

- [x] 0.1 完成 Requirements 与 Research
  - 冻结 chunk identity、exact membership、不重叠 partial partition、Unallocated、split/merge/repartition 和 workspace rebase。
  - 冻结 assignment/permission/progress、namespaced metadata、audit/undo、conflict 和 `current_chunk` 语义。
  - 记录 Multi-Document C2 启动门、C3/C4 隐性依赖和 ProjectPackage/provider/account/TM 负向边界。

- [x] 0.2 完成 Design
  - 设计 frozen DTO、ID/digest/limits、topology 两阶段事务、local metadata publication、actor capability、permission decision、progress/rebase 与 conflict/undo。
  - 明确 ProjectPackage v1 不变，Chunk metadata 只使用独立 namespace/store；未来 transport 仍等待 Sync/extension 治理。
  - 记录本轮无需新 ADR；如果后续设计改为 ProjectPackage v1 extension、重叠多写者或自动语义 merge，必须先起草后继 ADR。

- [x] 0.3 完成 Tasks、spec-local border 与实施状态
  - 保持 brief C1–C4 聚类责任，将 local metadata persistence 放在 C1，rebase/permission/progress 放在 C2，conflict/undo 产品编排放在 C3。
  - `border.md` 只保留长期红线与启动/降级条件；有语义增量的修正回写 Requirements / Design / Tasks；`spec.json` 是机器可读实施状态权威。

- [x] 0.4 核对风险与跨规格边界
  - 覆盖 Document-as-chunk、index/range identity、ProjectPackage v1 抢写、permission 只在 UI 生效、manager 默认扩权、detached/missing 静默删除、provider LWW 和 account 越界。
  - 核对 Requirements ↔ Design ↔ Tasks 映射、limits/error body-safety、Cluster 前置与 completion 条件。

- [x] 0.5 冻结 v1 语义与实施入口
  - 固定 v1 不重叠 partial partition、单 assignee、独立 metadata store、detached 只读/排除分母、current-head-only undo、local/reference actor 诚实边界和单 active plan。
  - 将 `ChunkLimitProfile v1` 作为单一版本化限制集，不拆成并行配置。
  - 确认 R/D/T 一致后将 `ready_for_implementation` 置为 `true`。

### Cluster 0 完成门

- R/D/T/Research 对七项 v1 固定语义和跨规格边界表达一致。
- production/tests/Controller/Qt/Steering/current-source evidence 零 diff。

## Cluster 1：Chunk Identity、Membership、Split/Merge 与 Local Metadata

- [x] 1.1 建立 frozen chunk contracts 与 ID/digest/limit 规则
  - 落地 `AssigneeRef`、`ChunkSegmentRef`、`CollaborativeChunk`、`ChunkPlanSnapshot`、最小 `ChunkPlanBinding` / `ChunkScopeProjection`、topology preview/receipt、topology-only `ChunkManagerCapability` 与 stable errors。
  - `ChunkScopeProjection` 只含 project/plan/chunk identity、plan revision/digest、universe digest 与完整 exact member tuple；不含正文/presence/order/assignee、TMX/profile/carrier/destination/loss/receipt，不允许用 attached-only、search hits 或隐式 current UI state 代替。
  - 落地 owner-owned `issue_scope_projection(explicit_chunk_id, expected_plan_binding)` 与 `revalidate_scope_projection(projection)`（或 exact 等价 seam）；两者只签发/复验 active membership binding，不读取正文、不决定 payload/carrier，也不授予 exporter 读取 plan/store/metadata。
  - `AssigneeRef`/nullable `assignee` 在 C1 只冻结 future-compatible shape；所有 C1 created/decoded/previewed/persisted/cold-reopened active chunks 必须 `assignee=None`，preview/receipt/audit assignment counts 必须为 `0`。
  - 最小 manager substrate 只绑定 project/session/plan/operation/single-use，不授予 assignment 或 target/confirmed 编辑权；current-source 只用诚实标记的 local/reference handle。
  - `chunk_plan_id` / `chunk_id` 使用 domain-separated 稳定 ASCII token；membership 只使用 exact composite identities。
  - 落地 Segment Universe digest、plan semantic digest、canonical member order 和 `ChunkLimitProfile v1`。

- [x] 1.2 建立 create/rename/reorder/split/merge/move/dissolve 领域服务
  - active chunks 形成 disjoint partial partition，Unallocated 从 universe 差集派生。
  - 所有 topology mutation 使用 private single-use plan + body-safe preview/apply，复验 workspace/session/plan/manager capability。
  - create/rename/reorder/move/dissolve 的 candidate/preview/published snapshot 保持全部 active chunks `assignee=None`、assignment counts `0`。
  - split/merge 退役源 IDs、签发新 IDs，children/result 强制 `assignee=None`；parent 预填、child/result 选择与 inheritance policy 只在 C2 激活；move 原子保持 union/disjoint；dissolve 不修改 workspace。
  - 任一 non-null assignee、assignment count `> 0` 在 private candidate/publication 前以 `CHUNK.CONTRACT_INVALID` 拒绝；assign/reassign/unassign command 以 `CHUNK.ASSIGNMENT_UNAVAILABLE` 拒绝；plan/store/LKG/audit/workspace 零 mutation。

- [x] 1.3 建立 namespaced local metadata store
  - 冻结 `localcat-collaborative-chunk-metadata-v1` exact canonical JSON、strict decoder、digest 与 audit record chain。
  - C1 canonical JSON 的 active chunk `assignee` 必须为 `null`；strict decoder 对字段缺失、non-null/object 或派生 assignment count `> 0` fail closed，不静默清洗。
  - 实现 candidate → cold validate → revalidate → journal/LKG → atomic replace/durability → cold readback → cleanup proof。
  - candidate validation、cold decode、publish revalidation 与 cold readback 各自复验 `assignee=None`/counts `0`；拒绝发生在 journal/LKG/replace/audit publication 前并保留原 store/LKG。
  - 不导入或调用 ProjectPackage physical/logical owner；证明 ProjectPackage v1 bytes/schema/golden 不变。

- [x] 1.4 闭合 C1 contract/property/fault/architecture tests
  - 使用正式 exporter 产生至少两 Documents 且跨文档重复 local ID 的真实 ProjectPackage，冷开后创建连续/离散/跨文档 chunks。
  - 建立正向阶段矩阵：create → decode → preview/candidate → persist → cold readback/cold reopen 每一点都断言所有 active chunks `assignee=None` 且 assignment counts 为 `0`；split children 与 merge result 同样为 `None`。
  - 建立负向阶段矩阵：non-null assignee、count `> 0`、assign/reassign/unassign 分别在 command/domain/decode/store 入口于 candidate/publication 前失败，并断言 plan/store/LKG/audit/workspace bytes/digest/revision 均不变。
  - 覆盖 rename/reorder/reopen、duplicate/overlap/foreign/unknown/stale/limit、split/merge/move 故障、forged/replayed/retired manager capability，并证明 management capability 无 target/confirmed mutation 能力。
  - 覆盖 scope projection exact fields/full membership、issue→plan drift→revalidate、stale/retired/foreign 拒绝与 forbidden-field scan；证明后续消费者无需读取 chunk store/metadata 或借用 search hits 才能取得并复验明选 membership。
  - 覆盖 metadata duplicate key/extra field/non-null assignee/non-zero assignment count/digest/size/depth、stage/replace/readback/cleanup/cold recovery，并在真实冷重开后复验未分配状态及扫描 body leakage。
  - 累计 architecture 和边界证据证明 chunk 没有变成 Document、ProjectPackage、Parser、TM 或 provider owner。

### Cluster 1 完成门

- 验证目标：真实 ProjectPackage 冷开后，exact membership 经 rename/reorder/reopen 不变；create/decode/preview/persist/cold-reopen 全程 `assignee=None`/assignment counts `0`，所有 assignment intent 在 candidate/publication 前零 mutation 拒绝；split/merge/move 失败零部分发布，local metadata 可冷恢复且 ProjectPackage bytes 不变。
- C1 累计实施验证与边界检查收束后才进入 C2。

## Cluster 2：Assignment、Permission、Progress 与 Workspace Rebase

- [x] 2.1 建立 actor/assignment 合同与 reference composition
  - 身份 owner 只交付 private authenticated actor handle；chunk metadata 只持久 opaque authority/subject ref。
  - 落地每 chunk 零/一 assignee、assign/reassign/unassign 两阶段事务与 plan revision-based revocation。
  - 将 authenticated actor/assignee/assignment-edit capabilities 组合到 C1 topology-only manager substrate，不将 manager 默认扩权为编辑者。
  - 提供诚实标签的 local/reference actor port 作为 current-source harness，不实现或宣称 account/auth/provider。

- [x] 2.2 建立 chunk permission service
  - 进入明选 chunk 切面后，只有 exact assignee + active current chunk + attached member 可编辑 target/confirmed；“全部章节（未选择分工）”保持 Workspace 整项目编辑。
  - outside current、Unallocated、not-assignee、detached、stale 返回明确 read-only decision/safe code。
  - manager 只管 topology/assignment/rebase/undo，不默认编辑；无 active plan 时不改既有个人模式。

- [x] 2.3 建立 chunk progress 投影
  - 从 current workspace 派生 attached total/unfilled/draft/confirmed/detached，不持久 counters。
  - detached 排除 completion denominator，空 attached set 不伪造 100%。
  - project/document progress 继续由 workspace 拥有，不得重命名或覆盖。

- [x] 2.4 建立 exact Chunk rebase preview/apply
  - same identity unchanged/source_changed 保留，attached→detached 保留但只读，missing 每项要求 release/cancel，new 保持 Unallocated。
  - preview 绑定 old/new universe digest、workspace session/revision/composition revision、plan revision 和 single-use capability，publication 精确复验完整 Workspace binding，失败零 plan/workspace mutation。
  - 只在 live Workspace owner 复验已发布 transition 后捕获同 store/同锁 pending intent；cold reopen 从 intent 续做，不提升携带 DTO；sidecar 使用显式 v1 byte limit 与 source-changed indices。
  - 单个空 chunk 要求显式 retire；全空拒绝 rebase 并只允许后续显式 `DISSOLVE_PLAN`；同一 live session 连续第二次 reconciliation 即使恢复相同 universe 也在 v1 fail closed，cold 新 session 也只在 composition revision 仍为 `0` 时允许续做。
  - 不导入 Parser、不打开 origin、不按 index/text/display name 猜测。

- [x] 2.5 闭合 C2 permission/rebase/fault/architecture tests
  - 覆盖 assignment 变更后旧 edit capability、manager 非默认写、多 assigned chunks 的 current selection，以及所有 read-only reason。
  - 在 domain/controller-like mutation port 层验证越界写拒绝，不以 UI disabled 代替。
  - 覆盖 source_changed/new/missing/detached/reorder/target-only 的 rebase/progress 矩阵和 body-safe reports。
  - 累计 architecture 和边界证据证明未建立账号、source reconciliation、ProjectPackage、TM 或 provider 第二权威。

### Cluster 2 完成门

- 验证目标：只有 assigned current attached member 可写，其他范围明确只读；progress/rebase 对 exact current workspace 语义诚实，任何 stale/identity 故障不改 plan 或 workspace。
- C2 累计实施验证与边界检查收束后，且 Multi-Document C3 合同与回归已稳定，才进入 Chunk C3。

## Cluster 3：Controller Scope、Conflict 与 Undo

- [x] 3.1 确认 Multi-Document C3 上游合同
  - 复用已通过回归的 workspace session/revision/composite navigation/search/save API 与 current-source implementation，不根据旧设计草案预写并行 Controller。
  - 写出集成验证锚点：一个 target/confirmed 目标业务 API 必须实际消费 chunk permission decision。

- [x] 3.2 接入 workspace/chunk/actor/current-chunk session
  - ProjectPackage open 成功后再打开 exact project-bound chunk metadata；missing 表示无 plan，invalid/mismatch 不覆盖 workspace。
  - current chunk 只接受当前 plan active ID，切换换 capability epoch；retired/foreign/stale 不改导航或 target。
  - 所有 target/confirmed mutation 命令在首次 workspace mutation 前重验 actor/session/plan/current chunk/segment。

- [x] 3.3 以 versioned contract 增加 `current_chunk` search scope
  - 保持 `current_document` / `entire_project` exact 语义；`current_chunk` 只遍历 exact active membership 并按 current workspace navigation order 投影。
  - 复用同一 matcher pipeline、composite hit identity、field/offset/stale guards，不复制 Match Case/Whole Word/TM 语义。
  - `current_chunk` search 与后置 export 都消费 owner-issued `ChunkScopeProjection`，但 search hit 不得反向成为 export scope；Workspace 才负责 join 当前 presence/content/navigation order。
  - entire-project hit 附带当前只读/可编辑投影，导航不授权。

- [x] 3.4 建立 metadata conflict preview/apply 与 current-head undo
  - 分类 identical/fast-forward/stale/diverged/foreign/universe-mismatch，仅 exact verified fast-forward 可直接 apply。
  - diverged 禁止 auto-union/LWW/timestamp merge，只提供保留 current、完整替换 incoming 或取消的明示 preview。
  - undo 仅指向 current audit head，恢复 verified previous snapshot并以新 revision/receipt 发布。
  - v1 仅恢复不需要复活 append-only retired ID 的 current head；退役型 head 结构化拒绝为 `CHUNK.UNDO_UNAVAILABLE`。

- [x] 3.5 闭合 C3 integration/fault/compatibility 验证
  - 覆盖 current chunk 切换、stale actor/edit/search hit、assignment 竞态、metadata divergence、non-head undo、workspace switch 与无 active plan。
  - 证明 Controller 不解码 chunk metadata/ProjectPackage/codec-private，search 不复制 matcher。
  - 累计 C1–C3 diff 的 architecture 和边界证据必须确认 provider/account/Qt 未抢跑。

### Cluster 3 完成门

- 验证目标：`current_chunk` 使用 Multi-Document 目标搜索 API 返回 exact member hits，任一 chunk 外 mutation 在 Controller 业务 API 失败，divergence/undo 保留当前 workspace/plan。
- C3 累计实施验证与边界检查收束后，且 Multi-Document C4 产品面与回归已稳定，才进入 Chunk C4。

## Cluster 4：Qt 与 Current-source Acceptance

- [x] 4.1 确认 Multi-Document C4 上游产品面
  - 复用已通过回归的章节 selector/divider/navigation/search/save 产品面，Chunk UI 只作正交增量。
  - 不合并 Document/chunk selector，不在 Qt 直接读 workspace/package/chunk metadata。

- [x] 4.2 实现 chunk selector、manager 与 progress 投影
  - 显示 active chunks、current chunk、assignee safe label、membership/progress/detached/Unallocated counts。
  - 首页不放 Chunk 控件；Project 下拉提供“协作分工管理 / 当前分工”，未选分工时保持整项目可编辑，选定分工后编辑、浏览/校对与 Document 文件夹均只投影其跨文档 exact members。
  - 高级 create/move/release/精确拆分由 manager 签发一次性 body-safe exact-identity 请求，暂时复用浏览/校对全宽双语表完成起点/终点、离散多选、清除和“选择全部尚未分工”；会话不导航、不发布，返回后恢复原 mode/document/segment/search/current chunk。直接拆分项目/源分工不依赖该预选。
  - 将原子 create/split/merge 收束为直接的“拆分项目 / 拆分分工 / 合并分工”主路径：动态 2–N 分组在单次 publication 中闭合，并显式处理 child/result assignment；其余 rename/reorder/move/dissolve/assign/rebase/undo 仍通过 Controller preview/apply。
  - 所有高影响变更在 apply 前显示 exact counts/assignment/blockers 并要求显式确认。
  - 本 Cluster 不增加项目/资源导出按钮或 TMX/ResourcePackage 控件；后置项目导出 UI 只能使用 Controller-issued scope projection。

- [x] 4.3 实现 current-chunk search 与只读反馈
  - 搜索文案为“当前章节 / 当前分工 / 搜索全部章节”，越界 hit 可导航但只读。
  - target/confirmed UI 显示 outside/unallocated/not-assignee/detached/stale 原因，同时用 Controller 负向 command test 证明不可绕过。
  - local/reference actor 显示诚实非账号标签，不提供 provider/sync/auth 控件。

- [x] 4.4 使用真实 ProjectPackage + metadata 完成 current-source acceptance
  - 从 C2 正式 exporter 生成真实双 Document package，冷开后创建跨文档连续/离散 chunks，保存 metadata并冷重开。
  - 运行 assign/switch/edit/read-only/search/progress/reconcile-rebase/split/merge/conflict/undo/close-reopen 完整 journeys。
  - 证明 chunk-only operations 不改 ProjectPackage bytes、workspace target/dirty 或 codec-private；实际 target edit 仍由 workspace/package 后续保存。

- [x] 4.5 重签 evidence 并完成治理收尾
  - 在 final runtime roots 运行 chunk contract/topology/store/permission/rebase/controller/search/Qt/fault/acceptance 与无-plan legacy suites。
  - current-source 工具生成 evidence 并由 strict consumer 复读；evidence 后只允许不属于 source roots 的 Tasks/Steering/border completion 更新。
  - 同步真实 structure/tech/roadmap/spec ownership，并以 final cumulative diff 的可重放验证闭合 Feature 验收。

### Cluster 4 完成门

- 真实双 Document ProjectPackage + namespaced metadata 冷重开和 Qt 完整业务 journey 通过；越界只读在 Qt 与 Controller 双层都得到证明。
- ProjectPackage v1、provider/sync、account/auth、TM/Fuzzy、codec-private、重叠审校继续保持负向边界。
- 最终语义提交：`feat(chunks): 建立协作分工工作流`。

## 跨 Cluster 对抗检查

- [x] A. 身份漂移防线
  - 禁止用 Document/display/order/path/index/text 作 chunk/member identity；禁止只用 local segment ID。

- [x] B. Authority 防线
  - chunk 不拥有 workspace content/reconciliation/save、ProjectPackage、Parser/codec、identity authentication、provider、TM/TMX payload、ResourcePackage carrier/Fuzzy；scope projection 不含 payload/carrier/destination/loss。

- [x] C. Permission 防线
  - 权限必须在 Controller mutation boundary 复验；manager 不默认编辑；未知/stale 一律只读。

- [x] D. Persistence/Sync 防线
  - ProjectPackage v1 exact 不变；chunk local snapshot/scope projection 不是 ProjectPackage/ResourcePackage；未来 sync 只搬运 exact bytes 并调用 chunk validate/preview/apply。Project/chunk TMX 为后置 direct artifact，ResourcePackage 只包装 managed resource。

- [x] E. 验证漂移防线
  - C1 必须使用真实 ProjectPackage 冷开 workspace；C3 必须调用 Multi-Document 目标 Controller/search API；C4 必须证明真实 command 被权限拒绝，不以 fixture-only/disabled widget 代替。

## 明确禁止

- `spec.json` 尚未 `ready_for_implementation` 时修改任何 production/tests/Controller/Qt/Steering/evidence。
- 在 Multi-Document C3/C4 之前复制并行 Controller/search/Qt workspace authority。
- 把 Document 当成 chunk，或持久 range/index/display/path/text 作 membership identity。
- 让 active chunks 重叠，或在没有新 policy/ADR 时实现同段多写者。
- 在 ProjectPackage v1 增加 chunk member/field/extension，或让 chunk store 调用 ProjectPackage save/apply。
- 在 Qt 禁用控件但保留可绕过的 Controller mutation。
- 从 display label 猜 assignee，保存 credential，或宣称 local/reference actor 是安全账号认证。
- 按 index/text 猜测 missing/new member 的对应关系，或在 divergence 上自动 union/LWW/timestamp merge。
- 让 chunk 权限授权 source write-back、ProjectPackage、TM/Fuzzy、codec-private 或 provider 操作。
- 让 Chunk 过滤 export inclusion、实现 TMX writer/direct publication、选择 ResourcePackage carrier，或让 exporter 解析 chunk metadata/store/search hits 重建 scope。
