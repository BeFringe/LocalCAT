# 需求文档

## 简介

LocalCAT 的多文档工作区已用稳定 `Project → Document → Segment` 身份和 ProjectPackage 手工闭环保存项目内容，但 Document 仍然是内容/章节结构，不是协作分工单元。如果把文件或章节直接当成 chunk，跨章节分工、离散段落分配、拆分/合并、权限与进度都会反向污染项目身份和保存语义。

本规格在不改变任何 Project/Document/Segment 规范身份的前提下，建立可选的协作 chunk 视图。Chunk 仅引用稳定 `(project_id, document_id, local_segment_id)`，拥有 chunk identity、segment membership、split/merge/repartition、assignment、permission、chunk progress，以及供后续消费者使用的最小 exact scope projection；它不拥有 Document identity、ProjectPackage 保存/传输、source reconciliation、provider、TM/Fuzzy、TMX payload/export、ResourcePackage carrier 或 codec-private 语义。

## 范围边界

- **范围内**：chunk/plan 稳定身份；连续范围与离散 segment 集合的 exact membership；创建、命名、重排、拆分、合并、成员转移与解散；单 assignee 分配；基于当前 chunk 的编辑权限与越界只读；chunk 进度；workspace reconciliation 后的成员关系对账与更新 preview/apply；本地 namespaced chunk metadata、审计 receipt、冲突和安全撤销；不含正文/导出语义的 `Chunk_Scope_Projection`；Controller `current_chunk` scope 与 Qt 产品投影。
- **范围外**：Project/Document/Segment 解析、铸造、保存、正文 materialization 或 source reconciliation；ProjectPackage manifest/carrier/export/import/apply/recovery；TMX grammar/profile/inclusion/loss/direct artifact publication；ResourcePackage manifest/carrier/preview/apply/receipt/cold reopen；账号注册、身份认证、密码、凭据或用户目录；S3/WebDAV/provider、远端 listing、条件写、加密或传输日志；实时光标、聊天、在线 presence 和 OT/CRDT；TM/术语资源、Fuzzy 授权、QA 规则、审校工作流、计费和外包市场。
- **上游依赖**：`multi-document-project-workspace` 完整 Cluster 2（C2A aggregation/reconciliation、C2B save/recovery、C2C 真实 ProjectPackage 手工闭环）必须已通过。只有 Cluster 1 identity 不足以开始 chunk runtime。
- **后续接入依赖**：Chunk C3 的 Controller/search 复用多文档 C3 的稳定 session/search API；Chunk C4 的 Qt 产品验收复用多文档 C4 workspace UI，不在本规格中复制它们。

### Scope Lineage（范围沿革）

- 本文是 `collaborative-job-chunks` 的首份正式 Requirements；同目录 `brief.md` 继续作为需求来源，不单独授权实施。
- 实施顺序和可开始状态由 R/D/T 与 `spec.json` 表达；过程讨论不作为额外规格层。
- ProjectPackage v1 是已批准且封闭的项目持久权威，其 schema 不包含 chunk 字段。本规格的 chunk metadata 是独立 namespace 与语义权威，不得为了持久或同步而宽松读写 ProjectPackage v1。
- Chunk metadata 与 `Chunk_Scope_Projection` 都不是 ResourcePackage payload 或默认 transport candidate。后续 `tmx-context-interchange` 只能消费最小 scope projection，由 Workspace join 当前正文/presence/order；`language-resource-portability` 的 ResourcePackage 仍只包装一个 managed resource 快照。
- 未来 sync 可以搬运本规格批准的 exact chunk metadata bytes/digest，但必须另经 ProjectPackage 新 schema/namespaced extension 或独立 companion transport 批准；provider 仍不得解释 membership/permission。

## 术语

- **Chunk_Plan**：一个 Project 的可选协作视图，拥有稳定 `chunk_plan_id`、单调 revision、有序 chunks 和独立语义 digest。
- **Collaborative_Chunk**：引用一组 exact Segment_Ref 的协作对象，拥有稳定 `chunk_id`、显示名、顺序和可选 assignee。
- **Segment_Ref**：对上游规范 segment 身份的 exact 引用，逻辑上为 `(project_id, document_id, local_segment_id)`；不包含 source/target/speaker。
- **Segment_Universe_Digest**：对当前 Project 中 exact Segment_Ref 与 attached/detached presence 的载体无关摘要；不包含 display order、source/target 正文或 ProjectPackage 物理路径。
- **Assignee_Ref**：由外部身份 owner 认证并由 Controller 会话签发的 opaque authority/subject 引用；chunk 层不保存凭据或验证密码。
- **Unallocated**：当前 Segment Universe 中未属于任一 active chunk 的 attached segments；它不是伪 chunk 或 Document。
- **Chunk_Rebase**：在上游 workspace reconciliation 发布后，将旧 membership 与新 Segment Universe 做 exact identity 对账的 preview/apply；它不运行 source reconciliation。
- **Chunk_Scope_Projection**：对一个明选 active chunk 签发的最小只读合同，只含 project/plan/chunk 身份、plan revision/digest、segment universe digest 与完整 exact Segment_Ref tuple；不含正文、presence、导航顺序、assignee、TMX/profile/carrier/destination/loss/receipt。Workspace 负责 join 当前事实，后续消费者负责自己的 inclusion 语义。

## 需求

### Requirement 1：分簇实施顺序

**目标：** 作为项目维护者，我希望 Chunk 在稳定项目持久层之上分阶段推进，以便协作语义不会抢回 workspace、Controller 或 provider 权威。

#### 验收标准

1. The Chunk 推进顺序 shall 保持 Cluster 0 → 1 → 2 → 3 → 4，每一簇完成后都必须有与本簇目标相匹配的可重放验证，再进入下一簇。
2. While `spec.json` 的 R/D/T 状态尚未 `ready_for_implementation`, the implementation shall 不得修改 production、tests、Controller、Qt、ProjectPackage schema 或 current-source evidence payload。
3. The Cluster 1 runtime gate shall 要求 `multi-document-project-workspace` C2A/C2B/C2C 的正式合同、真实双 Document ProjectPackage 冷重开与累计可重放验证已通过。
4. When Cluster 1 实施时, the implementation shall 只交付 chunk plan/identity、exact membership、create/split/merge/repartition/dissolve、独立 metadata persistence，以及这些 topology 变更所必需的最小 manager/capability substrate；C1 可冻结 optional assignee 字段 shape，但所有 C1 runtime-created/decoded/previewed/persisted/cold-reopened active chunk 都必须 `assignee=None`、assignment counts 必须为 `0`，且任何 non-null assignee 或 assignment command 必须在 publication 前 fail closed 且零 mutation。该 substrate 不授予 target/confirmed 编辑权，也不进入 assignment/permission 或 UI。
5. When Cluster 2 实施时, the implementation shall 交付 assignment、permission、progress、workspace-rebase 和越界只读，不实现账号或远端传输。
6. Before Cluster 3 接入时, the implementation shall 确认多文档 C3 Controller/session/search contract 已稳定并通过回归；before Cluster 4 接入时, it shall 确认多文档 C4 Qt workspace 产品面已稳定并通过回归。

### Requirement 2：Chunk identity 与 exact membership

**目标：** 作为项目管理者，我希望章节改名、重排、包移动或冷重开后仍然引用同一组段落，以便分工不随展示投影漂移。

#### 验收标准

1. The Chunk_Plan shall 持有非空稳定 `chunk_plan_id`、exact `project_id`、单调 revision 和 `segment_universe_digest`，且一个 active Project session 最多发布一个 active plan。
2. The Collaborative_Chunk shall 持有 plan 内唯一的稳定 `chunk_id`；重命名、重排、assignment 变化和 ProjectPackage 绝对路径变化不得改变该 ID。
3. The active membership shall 是非空、唯一、项目内可解析的 exact Segment_Ref 集合；不得复制 source、target、speaker、index 或 display name。
4. The v1 active chunks 的 attached members shall 形成对当前 attached Segment Universe 的不重叠部分划分：一个 exact Segment_Ref 最多属于一个 active chunk，未被纳入的 attached segment 保持 Unallocated；rebase 后保留的 detached members 继续受全局不重叠约束，但不属于 attached partition。
5. When 用户以当前导航的连续范围创建/拆分 chunk 时, the Application shall 在当次 session/revision 下解析为 exact Segment_Ref tuple；持久合同不得仅保存起止下标。
6. When Document display name/order 改变或 ProjectPackage 被移动时, the chunk projection shall 按当前 workspace 导航顺序重新投影，但保持 plan/chunk/Segment_Ref identity 不变。
7. When 明选 active chunk 的 downstream scope 被请求时, the Chunk service shall 签发绑定 exact project、chunk plan id/revision/digest、segment universe digest 和 chunk id 的 `Chunk_Scope_Projection`，并包含该 chunk 的完整 exact membership，包括仍被保留的 detached refs；不得以 `current_chunk` UI 状态、search hits、metadata raw JSON 或 attached-only 过滤代替该合同。
8. The Chunk service shall 拥有 scope projection 的 issue/revalidate seam：首次签发要求 explicit chunk id 与 expected project/plan/revision/digest/universe binding；后续 publication 前可对原 projection 复验 active/non-retired membership 与同一 binding。该 seam 只验证身份/membership，不读取正文或决定 payload/carrier。

### Requirement 3：Create、split、merge 与 repartition

**目标：** 作为项目管理者，我希望能够原子地改变分工边界，以便任何失败都不会留下重叠、丢失或半拆分的成员集。

#### 验收标准

1. When 创建 chunk 时, the service shall 只接受当前 Unallocated 的 exact Segment_Ref，一次发布新 chunk 和 plan revision；任一 invalid/stale/duplicate member 导致零 mutation。
2. When 拆分 chunk 时, the service shall 要求两个或以上非空子集彼此不交叉且 exact union 等于原 membership，退役原 `chunk_id`并为每个子 chunk 签发新 ID。
3. When 合并两个或以上 chunks 时, the service shall 以它们的 exact union 创建一个新 chunk、退役所有源 ID，且不得因物理相邻、同 Document 或显示名而增减 member。
4. While Cluster 1 运行时, split 的所有 child chunks 与 merge 的 result chunk shall 强制 `assignee=None`；任何 non-null assignee 输入、非零 assignment count 或 assign/reassign/unassign command 都必须在 candidate/publication 前 fail closed 且零 plan/store/workspace mutation。
5. When 在既有 chunks 之间移动成员或释放为 Unallocated 时, the service shall 在单一 plan revision 中验证 source/destination/union/disjoint 不变量，禁止中间部分状态可见。
6. When 解散 chunk 或整个 plan 时, the service shall 只退役协作 metadata并将 attached members 返回 Unallocated；Project/Document/Segment、target、confirmed、dirty 和 ProjectPackage bytes 不得变化。
7. The topology service shall 只接受当前 project/session/plan 签发的 private single-use manager capability；该 capability 只授权 topology/metadata operation class，不得被 target/confirmed mutation 或 assignment 当作编辑权。
8. When 尚无 active plan 且用户选择按项目拆分时, the service shall 在一次 `CREATE` publication 中把当前全部 attached Unallocated members 按项目顺序分为 2–N 个连续、非空 chunks，不得 round-robin 或随机分散成员；不得以“先建一个 chunk、再 split”的两次可见 revision 模拟一步操作。When 拆分既有 chunk 时, the Application shall 可从该 chunk 的完整 exact membership 按同一项目顺序生成 2–N 个连续动态子组，不要求用户先在编辑首页选齐成员；每个已分配 child 的 inherit/Unassigned 仍须明示。

### Requirement 4：Workspace 变化后的 Chunk rebase

**目标：** 作为协作译者，我希望 source reconciliation 后不会把旧分工贴到错误段落，以便身份不能重关联时必须显式处理。

#### 验收标准

1. When 上游 workspace 仅改变 target、confirmed、display name 或 Document order 而 Segment Universe 不变时, the chunk plan shall 保持 compatible，不得重铸 chunk 或 member identity。
2. When 上游 reconciliation 对同一 Segment_Ref 分类为 `unchanged` 或 `source_changed` 时, the chunk membership shall 保持；chunk 层不得改变 source、target 或 confirmed。
3. When 上游保留 `detached` segment 时, the rebase preview shall 保留 exact membership、显示 detached count，并将该段落标记为只读且不计入完成率分母。
4. When 旧 member 在新 workspace 中缺失时, the rebase preview shall 按 exact Segment_Ref 报告 `missing` 并要求管理者显式释放或取消；未完成处置前不得发布新 plan。
5. When 新 workspace 增加 segments 时, the rebase preview shall 将它们标记为 `new_unallocated`，不得按位置、文本相似、Document 名或邻近 chunk 自动分配。
6. The Chunk rebase shall 只消费已发布 workspace/reconciliation facts，不得调用 Parser、读取 origin、解释 source fingerprint 或替代上游 reconciliation authority。
7. When reconciliation 发布新 workspace 时, the Workspace owner shall 签发 body-free published transition projection，绑定 previous/current project、发布 session/revision、只在 composition 变化时递增的 composition revision、workspace digest 与 Segment Universe digest，并包含完整 previous/current exact identity/presence entries 及 `source_changed` refs；public reconciliation preview 或仅含处置结果的 receipt 不得单独充当 rebase authority。
8. When Chunk 在 live Workspace owner 存活时捕获已发布 transition, the Chunk metadata store shall 以同一 rooted store/锁持久化不含 candidate/decision/assignee 的 pending rebase intent；cold reopen 只能从该 intent 续做。同一 live session 的 composition revision 再次改变时必须 fail closed，即使 universe 之后恢复相同也不得折叠跳过；不同 session 的 cold resume 只接受 composition revision `0`，该 session 一旦发布新 reconciliation 即使净 universe 恢复也必须拒绝旧 intent。若释放 missing members 使一个 chunk 为空，必须显式退役该 chunk；若使全部 chunks 为空，rebase 必须拒绝发布并要求显式 `DISSOLVE_PLAN`。
9. If Workspace reconciliation 已发布但 Chunk transition capture 失败, the Controller shall 仍安装已发布的 Workspace candidate 并将 Chunk 置为 `CHUNK.RECOVERY_REQUIRED` 只读状态；不得让 Controller revision/projection 停留在已失效的旧 owner 事实，也不得把已发布事务表现为回滚。
10. When same-project ProjectPackage replacement 会改变 active Chunk plan 的 Segment Universe 且没有 owner-issued transition seam, the Controller shall 在任何 package/carrier publication 前以 `CHUNK.REBASE_REQUIRED` 拒绝；不得先替换 active workspace 再留下无恢复路径的 metadata mismatch。

### Requirement 5：Assignment 与身份边界

**目标：** 作为项目管理者，我希望把 chunk 分配给明确译者，但不希望 chunk metadata 变成账号或凭据库。

#### 验收标准

1. The v1 Collaborative_Chunk shall 持有零个或一个 exact Assignee_Ref；一个 assignee 可同时被分配多个 chunks。
2. The Assignee_Ref shall 仅含稳定 authority/subject opaque identifiers，不包含密码、token、cookie、email 凭证、provider credential 或可用于仿冒认证的材料。
3. When 分配、改派或取消分配时, the service shall 要求当前 session 签发的 chunk-manager capability、精确 plan revision 和未变 workspace binding，成功后立即撤销旧 assignee 的未消费编辑能力。
4. The chunk layer shall 拥有 assignment 语义和权限判定，但不拥有账号创建、身份认证或 principal directory；未来 identity owner 只能交付已认证的 opaque actor capability。
5. If assignee authority 不可用或 actor capability 无法验证, the chunk layer shall body-safe fail closed，保留已存 assignment metadata，不得因 display label 相同自动改绑。
6. When Cluster 2 已激活 assignment semantics 且拆分已分配 chunk 时, the service shall 要求每个 child 的 final assignee/Unassigned 在 preview 中明示；When 合并 assignee 不完全相同的 chunks 时, it shall 要求管理者显式选择 result assignee 或 Unassigned，不得以顺序、最近更新或多数表决猜测。

### Requirement 6：编辑权限与越界只读

**目标：** 作为被分配 chunk 的译者，我希望只编辑当前分工范围，同时能以明确只读状态查看其他章节。

#### 验收标准

1. While 项目没有 active Chunk_Plan, the chunk integration shall 不得改变既有个人编辑、保存、搜索或确认行为。
2. When actor 打开分配给自己的 active current chunk 时, the permission service shall 只对该 chunk 内仍为 attached 的 exact Segment_Ref 授权 target 编辑和 confirmed 变更。
3. When 导航、搜索或浏览进入 current chunk 之外、Unallocated、他人 chunk 或 detached member 时, the Controller/Qt shall 保持内容可见但明确只读，并返回稳定原因码。
4. The chunk-manager capability shall 授权 chunk topology/assignment 操作，但不自动授权 target/source 编辑；管理者需要编辑时仍必须被显式分配并选中该 chunk。
5. The Controller shall 在所有可改变 target/confirmed 的入口重新验证 actor/session、plan revision、current chunk 和 Segment_Ref；不得只在 Qt 禁用控件后允许直接命令绕过。
6. The chunk permission shall 不得授权 source 改写、Document/Segment identity 变更、ProjectPackage 保存/安装、codec-private 读取、TM 写入或 provider 操作。

### Requirement 7：Chunk progress

**目标：** 作为译者和管理者，我希望 chunk 进度只反映它的成员，以便不与章节或整个项目进度混淆。

#### 验收标准

1. The chunk progress shall 在查询时从当前 workspace 和 exact membership 派生，至少返回 attached total、unfilled、draft、confirmed 和 detached counts；不得将可漂移计数作为持久权威。
2. The `confirmed` 与 target 空/非空状态 shall 消费上游 workspace 已批准语义，不得在 chunk 层重新定义翻译状态。
3. The detached members shall 单独计数、标记只读且不计入 completion denominator；Unallocated 或其他 chunk 的 segments 不得进入当前 chunk progress。
4. The project/document progress shall 继续由 workspace 拥有；chunk 层可同时投影上游数值供 UI 对照，但不得覆盖或重命名其语义。

### Requirement 8：Namespaced metadata、审计 receipt 与安全撤销

**目标：** 作为需要重开和复核分工的用户，我希望 chunk 操作可持久、可审计且能安全撤销，但不改写项目包。

#### 验收标准

1. The chunk metadata shall 使用独立 namespace/schema，绑定 `project_id`、`chunk_plan_id`、plan revision、segment universe digest、chunks、assignment 和 audit head；不得嵌入或宽松改写 ProjectPackage v1 manifest/member。
2. When 本地保存 chunk metadata 时, the repository shall 先生成完整 candidate、复验 schema/digest/base revision，再以单一发布点替换；失败保留 last-known-good metadata 和 ProjectPackage bytes。
3. When 任一 membership/topology/assignment/permission-relevant 操作成功时, the service shall 返回 body-safe `ChunkOperationReceipt`，绑定 operation id/action、actor ref、base/published revision、before/after plan digest、affected chunk IDs 和 counts。
4. The audit history shall 记录每个成功发布的语义操作及其 digest chain，不包含 source/target/speaker、绝对路径、凭据或 codec-private bytes。
5. When 用户撤销当前 audit head 时, the service shall 在 exact plan/workspace binding 下恢复已验证的 previous snapshot，以新的单调 revision 和新 receipt 发布；不得倒退 revision 或重写历史。
6. If 待撤销操作不是当前 head、workspace universe 已不兼容或 previous snapshot 无法完整验证, the service shall 拒绝撤销并保持当前 plan。

### Requirement 9：并发、stale 与冲突语义

**目标：** 作为可能同时编辑分工计划的管理者，我希望冲突显式失败，以便不会自动丢失分配或扩大编辑权限。

#### 验收标准

1. The every mutating command shall 绑定 project/session、chunk plan id/revision/digest、segment universe digest 和 single-use capability；任一变化在首次 mutation 前拒绝。
2. If 两个 metadata snapshots 具有相同 base 但不同 after digest, the chunk service shall 标记 `diverged`并拒绝自动 union、last-writer-wins 或按时间戳选择。
3. When assignment 或 membership 在编辑命令签发后变化时, the edit shall 以 `CHUNK.PERMISSION_STALE` 或更精确 safe code 零 mutation 失败，不得使用旧 capability 扩大权限。
4. The chunk layer shall 拥有 membership/assignment divergence 的语义 preview 和显式选择；未来 sync 只拥有远端 listing、conditional transfer 与 provider conflict orchestration，不得自行合并 chunk payload。
5. If 冲突未解决, the Controller/Qt shall 显示 body-safe 阻断和可重试状态，保留当前 plan、workspace target/dirty 和已发布 metadata。

### Requirement 10：Controller 会话与 `current_chunk` 搜索

**目标：** 作为译者，我希望在自己当前 chunk 内导航和搜索，同时保留“搜索全部章节”的项目级语义。

#### 验收标准

1. When Chunk C3 接入时, the Controller shall 使用新的 versioned search contract 增加 `current_chunk`，而不改写已批准 `current_document` / `entire_project` 语义。
2. When 搜索 `current_chunk` 时, the search service shall 只遍历当前 workspace 中属于 exact active membership 的 segments，按当前项目导航顺序返回带 Segment_Ref 的 hits，并复用同一 matcher pipeline。
3. When 搜索 `entire_project` 时, the results shall 继续包含全项目命中，但每个 hit 必须投影当前 actor/current-chunk 的 editable/read-only 决策，导航不得隐式授权。
4. The Controller shall 只接受当前 project/session/plan revision 签发的 chunk/segment/actor capabilities；stale、forged、retired 或 cross-project ID 在改变 current selection/target/confirmed 前 fail closed。
5. The Controller/search shall 不得读取 ProjectPackage manifest/ZIP、解析 chunk metadata JSON、解码 codec-private member 或自行计算权限；它们只消费 chunk service 的 frozen projections。
6. The issued `Chunk_Scope_Projection` shall 不携带 workspace 正文/presence/order、assignee、TMX payload/profile、ResourcePackage carrier、destination 或 loss policy；任何后续 exporter 必须由 Workspace 对完整 membership join 当前事实，并在 materialization 前复验 project/session/plan/revision/universe binding。

### Requirement 11：Qt 协作视图与可访问反馈

**目标：** 作为桌面端用户，我希望看到当前分工、assignee、进度和只读原因，以便不会误以为越界编辑已保存。

#### 验收标准

1. When active plan 可用时, the Qt UI shall 显示有序 chunk 列表、当前 chunk、名称、assignee 安全标签、membership/progress/detached counts 和 Unallocated count。
2. The manager UI shall 通过 Controller 提供创建、重命名、重排、拆分、合并、成员转移、解散、分配和撤销，并在发布前显示 body-safe preview/冲突/影响数量。
3. When 当前 segment 不可编辑时, the Qt UI shall 禁用 target/confirmed mutation 并显示“当前 chunk 外”、“未分配”、“分配给他人”、“已分离”或“计划已过期”之一稳定语义，不得仅吞掉输入。
4. When split/merge/rebase/undo 失败时, the Qt UI shall 保留当前 workspace、navigation、target/dirty 和 chunk plan，显示 safe code 与重试/重新 preview 入口。
5. The Qt UI shall 不得提供账号注册、provider、实时协作、ProjectPackage extension 或 TM/Fuzzy 控件；也不得直接读写 chunk metadata carrier。
6. The editor home shall 不显示 Chunk 状态条、selector 或收起控件。Project 下拉 shall 提供“协作分工管理”入口和“当前分工”选择；选定 chunk 后，编辑与浏览/校对均只投影该 chunk 的跨文档 exact members，Document 文件夹只展示这些 members 所在文档并仍负责文档导航。
7. When create/move/release/精确拆分需要高级选段时, the manager UI shall 签发一次性 body-safe 选择请求并暂时隐藏，复用浏览/校对页的全宽双语表显示完整项目上下文。该会话只能选择请求允许的 exact identities，支持起点/终点连续范围、Shift/Command 离散多选、清除，以及在 create 中显式“选择全部尚未分工”；“尚未分工”只表示未归入 chunk，与未翻译或未确认无关。浏览/校对不得在该会话中改变 current document/segment、搜索条件、current chunk 或调用 topology preview/apply；完成或取消后须恢复进入前状态，完成时仅按项目顺序把 exact identities 返回 manager。该范围不得成为“拆分项目 / 拆分分工”的前置条件。
8. The manager UI shall 将“拆分项目 / 拆分分工 / 合并分工”作为直接主路径：动态 2–N 分组、完整源 membership 与 assignment 决策由同一操作页完成；合并支持一次选中全部可见分工，结果名称留空时由 Application 路由稳定默认名。高级原子操作可保留，但不得迫使用户先创建临时 chunk 才能拆分项目。操作字段应使用紧凑布局，发布预览紧随必要字段并利用剩余空间。

### Requirement 12：兼容性、安全与边界验收

**目标：** 作为现有 LocalCAT 用户，我希望协作视图不回归项目保存，也不把后续同步或账号能力当成已完成。

#### 验收标准

1. The implementation shall 保持无 active plan 的既有单 JSON/TXT 与多文档 ProjectPackage 打开、编辑、保存、冷重开、搜索、TM/术语和 dirty 行为。
2. When 仅修改 chunk metadata 时, the acceptance shall 证明 ProjectPackage artifact digest/bytes、workspace content、Document/Segment identity、source/target/confirmed 和 codec-private member 全部不变。
3. The chunk modules shall 不得导入 Parser codec/registry、ProjectPackage physical carrier、TM/termbase store、Fuzzy qualification、Qt 或 provider SDK；Qt 只通过 Controller 消费 frozen contracts。
4. When 验收 identity/membership 时, the acceptance shall 使用至少两个 Documents、跨 Document 重复 local segment ID、跨文档连续 chunk 和离散 chunk，证明只有 exact composite identity 生效。
5. When 验收 fault/conflict 时, the acceptance shall 覆盖 duplicate/overlap/foreign/missing member、stale plan/workspace/actor capability、split/merge 中断、assignment 竞态、metadata publish/readback 失败、divergent snapshot、rebase 未决和 undo 非 head。
6. If 任一阻断故障发生, the acceptance shall 证明 last-known-good chunk metadata、ProjectPackage/workspace、target/confirmed、dirty、navigation 和权限没有非授权变化。
7. The chunk metadata/report/log shall 不包含 source/target/speaker、codec-private payload、绝对路径、凭据、device key、TM evidence 或 Fuzzy qualification。
8. While 后续 sync/identity/ProjectPackage extension 尚未批准, the product shall 不得声称 chunk assignment 已跨设备共享，也不得以复制 live metadata store 代替已验证 snapshot/apply 语义。
9. The acceptance shall 证明 `Chunk_Scope_Projection` 不包含 TMX/profile/carrier/destination/loss 字段，stale/retired/foreign projection 在任何后续业务 publication 前可被结构化拒绝；后续消费者不得导入 chunk store、解析 metadata 或借用 search hit/current UI selection 重建 scope。
10. The Chunk Qt surface shall 不增加项目/资源导出事务或导出按钮；未来项目导出 UI 只能消费 Controller-issued scope projection。资源页 `⋮` 的 managed-resource 导出与项目/分工导出保持独立。

## 非功能约束

- 所有跨层合同使用 exact frozen dataclass 与 tuple；`bool` 不得被当作数值，未知 enum/schema/version 一律 fail closed。
- 成员计数、chunk 计数、audit 记录和 metadata bytes 必须在 materialize 前执行 versioned limits 与 checked addition。
- public preview/receipt/error/log 必须 body-safe；programmer fault 保持可观察，已知 stale/permission/storage 故障在 Application boundary 映射为稳定 code。
- chunk membership/permission 查询和本地 metadata 处理不得发起网络请求。
- 任何未知或 divergent 状态默认只读；不得用可用性降级为扩权。
