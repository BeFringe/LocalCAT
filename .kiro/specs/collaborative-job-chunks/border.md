# 设计约束清单 — Collaborative Job Chunks

## 设计来源

- `collaborative-job-chunks/brief.md`。
- `multi-document-project-workspace` Requirements/Design/Tasks，尤其是完整 C2、stable Segment identity、reconciliation 与 ProjectPackage authority。
- 已采纳 ADR-018/019。
- Chunk 只拥有 chunk identity、segment membership、split/merge、assignment、permission、chunk progress 与最小 scope projection；不拥有 Document identity/正文 materialization、ProjectPackage 保存/传输、provider、TM/Fuzzy、TMX payload/export、ResourcePackage carrier 或 source reconciliation。

## Critical Path

```text
Multi-Document C2 complete
  → Chunk C0 R/D/T + boundary baseline
    → C1 identity/membership/topology/local metadata
      → C2 assignment/permission/progress/rebase
        → [Multi-Document C3 complete] C3 Controller/search/conflict/undo
          → [Multi-Document C4 complete] C4 Qt/current-source acceptance
```

隐性依赖：C1 必须使用 C2C 正式 ProjectPackage 冷开的真实 workspace；C1 只能冻结 optional assignee shape，C2 才首次激活 assignment；C3 必须消费 Multi-Document C3 的目标 Controller/search API；C4 必须同时证明 Qt 只读投影和 Controller mutation denial。

## 红线

| 约束 | 验证锚点 |
|---|---|
| `spec.json` 尚未 `ready_for_implementation` 时 runtime/UI/tests/Steering/evidence 不得改动 | 只有 spec-dir diff |
| 完整 Multi-Document C2 是 C1 runtime 前置 | C2A/C2B/C2C 真实 ProjectPackage 冷重开与累计可重放验证 |
| Chunk 只引用 exact stable segment identity | 跨 Document 重复 local ID、rename/reorder/reopen、foreign/forged refs |
| Document 不是 Chunk | 连续/离散/跨 Document membership；不用 file/chapter 自动建 chunk |
| v1 attached members 是不重叠 partial partition | overlap 零 mutation；Unallocated 显式；detached 仍全局不重叠 |
| split/merge/move 是单 revision 原子变更 | exact union/disjoint/nonempty/retired ID；中断不发布部分拓扑 |
| C1 先有最小 management capability substrate | 只授权 topology/metadata operation；private/single-use/stale fail closed；绝不授 target/confirmed 编辑权 |
| C1 不激活 assignment | create/decode/preview/persist/cold-reopen 全部 active chunks `assignee=None`、assignment counts `0`；split children/merge result 强制 `None`；non-null/count `> 0`/assign-reassign-unassign 在 candidate/publication 前零 mutation 拒绝；parent 预填/选择只在 C2 |
| Rebase 不接管 source reconciliation | unchanged/source_changed 保留，detached 只读，missing 显式 release/cancel，new Unallocated；无 index/text 猜测 |
| Assignment 不是账号系统 | metadata 只存 opaque authority/subject；无 credential；local/reference actor 诚实标签 |
| Permission 在 Controller mutation boundary 复验 | 只有 assigned + current + attached 可写；manager 不默认编辑；UI disabled 不代替 command denial |
| Chunk 是 Project 切面，不是首页固定区 | 首页无 Chunk 控件；Project 下拉管理/选择当前分工；编辑、浏览/校对和 Document 文件夹共用 exact membership 投影 |
| 主拆分不依赖段落预选 | 无 plan 对整项目 2–N 均分，已有 plan 对完整源 chunk 2–N 均分；高级选段用一次性 exact-identity 请求复用浏览/校对全项目双语表，完成/取消恢复 mode/document/segment/search/current chunk，且浏览页无 topology preview/apply authority |
| Progress 只从 current workspace 派生 | attached total/unfilled/draft/confirmed/detached；detached 不进分母；无持久 counter authority |
| Chunk 只签发/复验 scope，不决定 export payload/carrier | owner issue/revalidate seam + 最小 `ChunkScopeProjection` 完整 exact membership；Workspace join presence/content/order；无 TMX/profile/carrier/destination/loss 字段 |
| Chunk metadata 不改 ProjectPackage v1 | 独立 namespace/store；chunk-only operation 后 package bytes/schema/digest exact 不变 |
| Sync/provider 不解释 membership/permission | 只搬运未来传输合同明确的 exact bytes/digest；divergence 返回 chunk domain preview；无 LWW |
| Audit/undo 不重写历史 | current-head-only，exact previous snapshot，new monotonic revision/receipt |
| Multi-Document C3/C4 不被复制 | Chunk C3/C4 复用上游 contract/current-source implementation |

## v1 固定语义

1. 不重叠 partial partition + Unallocated。
2. 每 chunk 最多一个 assignee。
3. 独立 namespaced metadata/local store，ProjectPackage v1 不变；chunk metadata/scope projection 不是 ResourcePackage payload 或默认 transport candidate，后续 transport binding 未批准。
4. detached 保留 membership 但只读且排除 completion denominator。
5. undo 仅对 current audit head，以新 revision 恢复不需复活 retired ID 的 previous snapshot。
6. 当前无 account 时只用诚实标签的 local/reference actor 验收权限引擎。
7. 一个 active Project session 只有一个 active Chunk Plan。

`ChunkLimitProfile v1` 的数值是同一版本化限制集，不拆成并行配置。

若改为写入 ProjectPackage v1、允许重叠多写者或跨端自动语义 merge，必须先起草后继 ADR 并重新设计相邻规格。

## 降级红线

不允许以“先只做标签”降级权限门，不允许以内存 fixture 替代真实 ProjectPackage/metadata 冷重开，也不允许把本 Cluster 要求静默后移。尤其不得以“字段已存在”为由在 C1 接受或持久 non-null assignee；C1 assignment 红线必须从输入到冷重开逐点成立。
