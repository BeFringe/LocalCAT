# Multi-Document Project Workspace 复审分簇

更新：2026-10-16；同步目录与 RPY 的实际能力及后续交付边界。

> 本文件只定义 `multi-document-project-workspace` 的实施/复审节奏，不代替 Requirements、Design、Tasks 或人工批准。brief 的 Promotion Cluster 1–4 保持编号；Cluster 0 只做治理与 current-source characterization，业务代码 NO-GO。

## 适用范围与增量顺序

Cluster 0–4 记录原始显式文件 intake 与 ProjectPackage 基线。后续 Requirement 13 扩展目录 metadata 预览、显式选择、单文件 profile 与目录树投影，按新增任务独立实施和验收；原 Cluster 的“不枚举/不启用 directory”只约束当时范围，不否定该增量，也不能作为增量已完成的证据。workbook 仍为后续范围。

依赖顺序保持 owner 分离：Project 先提供可消费的选择/profile 接缝，RPY 分别消费它们完成单/多 TL、源更新和格式导出；既有 package 与 JSONL/CSV 资源端口支撑 RPY 手工包旅程。只有该闭环完成才进入 Sync 实施。各项可并行范围以 owning Tasks 为准，不把完整目录增量机械设为单 TL codec 的前置。

RPY 格式导出沿 ADR-030/032 使用中立 Parser 与平台端口，按文件报告发布；缺失目录准备与文件确认分开。它不接管原 Cluster 2 的项目保存/LKG/receipt，也不开放其他格式 writer。

## 共同不变量胶囊

- Project/Document/Segment 身份由稳定 manifest ID 或规范化相对 `source_ref` 建立；display/sheet name、顺序、列表索引和临时绝对路径都不是 authority。
- Parser/Codec 只产生单文档内容、能力与诊断；workspace 才聚合多个 Document；Qt/Controller 不复制 grammar、manifest、package 或 reconciliation authority。
- `codec_private_member` 对 workspace、chunk、sync 与 Qt 始终 opaque；只有 owning codec 可解释或写回。
- directory 不虚构跨文件原子性，workbook 只允许完整文件原子替换；目录选择/格式导出按各自增量合同实施，不能从原始 origin 声明推导 writer，workbook 产品 profile 仍未启用。
- ProjectPackage 只承载项目；TM/术语 ResourcePackage 独立，不抽取共同 authority。
- chunk 统一后置到完整 Cluster 2；sync 只消费已批准 package；TMX、RPY、PO/POT writer、CONTEXT 均留给各自 owner。

## Cluster 0：R/D/T、ADR、Characterization 与人工批准

- **Tasks**：0.1–0.5
- **治理提交**：`docs(multi-document): 冻结 workspace 治理与基线`
- **共享风险**：根据 brief 猜 runtime、未批准即写业务代码、characterization 只覆盖 happy path、提前选择物理 carrier、把相邻未来项写成本规格能力。
- **对抗重点**：R/D/T/ADR-018/review/border 一致；current-source import/constructor/call/patch/serialization inventory；单 JSON/TXT open/save/reopen/fault/UI session 基线；production 零变化。
- **通过条件**：独立 reviewer 给出 APPROVED，项目 owner 明确批准并勾选 0.5；否则 Cluster 1 NO-GO。

## Cluster 1：身份与 Origin

- **Tasks**：1.1–1.4
- **实施提交**：`feat(workspace): 建立多文档身份与 origin`
- **共享风险**：显示名/顺序泄漏为身份、绝对路径不可移植、同名/重排碰撞、单 JSON 兼容路径被强制迁移。
- **对抗重点**：重命名/重排/同名、伪造/跨项目 composite ID、路径规范化与 traversal、reopen identity、旧入口 exact compatibility。
- **通过条件**：三类 origin 与 immutable identity closed-world；现有单 JSON 项目不依赖未来 package/UI 即可继续运行。

## Cluster 2：聚合、持久化与手工 ProjectPackage

- **Tasks**：2.1–2.8
- **实施提交**：`feat(workspace): 闭合 ProjectPackage 手工包事务`
- **共享风险**：reconciliation 按索引猜测、部分保存误报成功、逻辑 manifest 与物理 carrier 混权、preview 与 apply 使用不同输入、codec-private 被通用层解释、ProjectPackage 吞并 ResourcePackage。

### 2A：Aggregation / Reconciliation

- **对抗重点**：ordering 与 identity 正交；显式 JSON/TXT/PO/POT 选择列表逐输入 verified terminal且不枚举未选文件；source_changed/new/removed/unchanged/ambiguous/unresolved；target 保留/撤销确认；显式 removed 决策；stale reconciliation。
- **进入门**：Cluster 1 reviewer APPROVED。
- **退出门**：aggregation/reconciliation fault matrix 通过；合成中立 Document 不被冒充为真实多文档产品 substrate。

### 2B：Save / Recovery

- **对抗重点**：carrier-neutral candidate/LKG、逐 Document baseline、dirty、structured report、publication/readback/rollback/cold recovery faults；directory/workbook 只验证未来原子性红线，不启用 discovery/profile/writer。
- **进入门**：2A 对抗检查通过。
- **退出门**：save/recovery 协议在不依赖具体 carrier 的 fault harness 上闭合；未知/部分发布不清 dirty、不丢 LKG、不虚报成功。

### 2C：ProjectPackage Logical + Physical Closure

- **先决人工门**：任何 production 实现前，owner 批准目录、单文件 archive 或其他 carrier 决策及迁移/恢复语义；未批准为 NO-GO。
- **对抗重点**：manifest version/identity/member path/digest、opaque `codec_private_member`、ResourcePackage negative guard、truncation/traversal/tamper/TOCTOU、旧包覆盖、preview 后篡改与 import/apply 原子边界。
- **退出门**：至少两个 Document 的真实 ProjectPackage 完成 export→cold validate→preview→import/apply→cold reopen/receipt；故障不改变旧项目或旧包。

- **Cluster 2 累计通过条件**：2A/2B/2C 各自定点审查后，由独立 reviewer 审查完整累计 diff；只形成一次 Cluster 2 提交。
- **下游门**：`collaborative-job-chunks` 统一在完整 Cluster 2 后进入；现有 `language-resource-portability` brief 也只在已验证 package 原语后提升为独立 R/D/T，不共享 authority。

## Cluster 3：应用服务

- **Tasks**：3.1–3.4
- **实施提交**：`feat(workspace): 接入多文档应用会话`
- **共享风险**：stale session 身份穿透 mutation、项目 dirty 掩盖文档失败、搜索跨 scope 泄漏、Controller 复制 package/reconciliation/codec 权威、预埋 chunk UI。
- **对抗重点**：issued identity、session/generation replacement、save/reconcile race、current_document/entire_project、部分失败恢复、旧 controller journey。
- **通过条件**：Controller 只编排 Cluster 1–2 contracts；stale/forged/cross-project 输入在首次 mutation 前拒绝；没有 chunk/provider/TM authority。

## Cluster 4：Qt 与 Current-source Acceptance

- **Tasks**：4.1–4.4
- **证据提交**：`test(workspace): 重签多文档 current-source 证据`
- **共享风险**：UI 用 row/index 代替身份、窄布局隐藏失败、fixture-only package 冒充真实 substrate、把后置 directory/workbook profile 误报为产品支持、final roots 冻结后再改 runtime、旧单 JSON/TXT 回归。
- **对抗重点**：章节跳转/分隔/重排、search scope、dirty/save/recovery、真实 exporter→ProjectPackage→validator/preview/import/apply→cold reopen、current-source evidence strict reread。
- **通过条件**：Qt acceptance 使用真实 ProjectPackage substrate；最终 runtime、fault、acceptance 与 compatibility evidence 绑定同一 tree；独立 reviewer 给出 Feature GO。

## 提交与复审规则

1. 每个 Cluster 只形成一次阶段性提交并接受一次独立对抗 review；Cluster 2 的 2A/2B/2C 逐段检查但累计为同一个 Cluster 提交。
2. review 前记录 base/tip、累计 diff、共享不变量、current-source roots、focused/fault/acceptance results；reviewer 不以旧 evidence 或相同数字推断当前 tree 通过。
3. blocker remediation 只闭合 concrete finding，使用同一 reviewer 定点复验；不得重写已审计的前序 Cluster。
4. 暂存只列 owned paths；`.DS_Store`、用户样本、相邻 Spec、live SQLite/journal/sidecar/stage residue 不进入提交。
5. Cluster 0 无人工批准、2C 无 carrier 批准、final real ProjectPackage journey 缺失，任一情况均为 NO-GO。

## 相邻线冻结

- `language-resource-portability`：独立拥有 TM JSONL 与术语 CSV/v1 ResourcePackage、报告与冷重开，不与 ProjectPackage 共享 authority。原始手工包基线已闭合；POSIX 按 ADR-033 支持稳定本机目录与 LocalCAT 协调的资源生命周期；RPY 两设备手工包旅程仍按当前候选独立验收。
- `tmx-context-interchange`：TMX context/provenance/export；TMX 始终是 language resource。
- `rpy-project-codec`：RPY token/sidecar/writer；folder 聚合依赖 workspace，按 ADR-030 先完成 RPY 手工包闭环，再进入 sync。
- PO/POT writer：未来 format-codec 决策，不由 origin 或 ProjectPackage 推导。
- `feature5-ui-integration`（Integration TM surface）：CONTEXT 判断与“上下文一致”投影；本规格不增加 evidence 字段。

| 版本 | 日期 | 变更 |
|---|---|---|
| v1 | 2026-08-22 | 冻结治理、身份、2A 聚合调和、2B 保存恢复、2C ProjectPackage 闭环、应用与真实 ProjectPackage Qt acceptance 五簇。 |
