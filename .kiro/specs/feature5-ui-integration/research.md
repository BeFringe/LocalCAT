# 研究与设计决策

## Summary

- **Feature**：`feature5-ui-integration`
- **Discovery Scope**：Complex Integration
- **Key Findings**：
  - Feature 5 的 canonical retrieval、matcher 和 capability authority 已冻结，且其 Requirement 2 已批准首次迁移/激活与失败保全；精确 `dd7c9f…` 没有应用 composition root，也缺少该既有要求的 application-facing 首次 activation 公开高层方法。
  - mixed legacy/canonical 不能把 legacy JSONL 伪装成 `TMRetrievalService` handle；需要一个只做 exact compatibility 的 legacy port，并在 adapter 中完成最终 global top-10。
  - Core retrieval default publisher 使用 fail-closed sentinel expectation，不能接收批准 Gate C roots；正式 publisher 必须由 recomputation release 的同一 expectation 构造。

## Research Log

### Feature 5 TM 与 matcher frozen contracts

- **Context**：UI 要显示 match type、final similarity、双 source，并启用统一 Match Case/Whole Word 语义。
- **Sources Consulted**：精确 Feature 5 `tm_contracts.py`、`tm_retrieval.py`、`matcher_capability.py`、`text_matcher.py`，ADR-009～011。
- **Findings**：
  - `TMQuery` 冻结 raw query/speaker/context、minimum similarity、limit 和 resource order。
  - `TMResult` 强制 EXACT/CONTEXT 为 1.0 且双 source 相同；FUZZY 的 `similarity` 等于 Core evidence final similarity并要求 distinct matched source。
  - `QueryReport` 允许 resource-local failure；只有 closed proof failure 可与该资源的 exact/context 结果并存。
  - `TextMatcherState/TextMatcherCapability` 是唯一 matcher authority，UI 不得再保留 `MatcherReadiness/MatcherCapability`。
- **Implications**：UI frozen projection复用 Core `TMMatchType/TextMatcherState`，只输出安全 display facts，不暴露 scorer/proof。

### Mixed legacy/canonical 与排序

- **Context**：Requirements 要求 legacy exact-only 和 canonical exact/context/fuzzy 同时查询并只显示全局前十。
- **Sources Consulted**：`tm_retrieval.py` service mapping/sort/limit、`tm_engine.py` legacy/canonical facade、当前 `editor_controller.py` 和 `renpy_tm_compat.py`。
- **Findings**：
  - `TMRetrievalService` 要求每个 handle 有 query lease，legacy JSONL 不能直接加入。
  - Core 要求 canonical handle `order` 精确等于 canonical `TMQuery.resource_order` 内的位置；不能保留混合配置中的空洞全局序号。
  - canonical service 在跨 canonical resources 排序去重后应用 global limit。
  - legacy engine 是 source-LWW exact，每资源最多一个 direct/strict alias result。
- **Implications**：canonical cohort 连续编号；另存 resource id 到全局配置序号。adapter 合并两个 exact lane，随后保留 Core context/fuzzy order并只截取一次十条。

### Retrieval Gate C/D application composition

- **Context**：Store health 或默认 bool 不得开放 fuzzy；UI 又必须实际消费 Feature 5 capability。
- **Sources Consulted**：`tm_retrieval_capability.py`、`tm_retrieval_validation.py`、`tm_benchmark_gate.py`、ADR-009/010。
- **Findings**：
  - default retrieval publisher 绑定 sentinel digests，只能作为关闭初态；批准 roots 产生的 manifest 与其 expectation 不同。
  - `recompute_retrieval_validation()` 返回配对的 `expectation + manifest`；正式 publisher 必须由该 expectation 构造。
  - Gate D 只有 `run_benchmark_gate_d()` 产生的 owner-issued result 可经 `publish_retrieval_capability_gate_d()` 刷新同一 publisher。
  - Gate D evidence 不能在下一进程重铸 receipt；重复启动必须新跑且使用新 absent evidence path。
- **Implications**：bootstrap 先提供 exact-only service；后台 Gate C 成功后原子换入新的 evaluator/publisher/service，Gate D 再刷新同一 publisher。所有失败保持当前较低能力。

### Feature 5 Requirement 2 首次 canonical activation 公开合同缺口

- **Context**：Feature 5 Requirement 2 已要求迁移可预检/可重试、只发布完整 active version，并在首次激活失败时保留 JSONL 兼容能力；Integration Requirements 5 把该已有 Core 能力接入用户显式操作。
- **Sources Consulted**：`tm_migration.py`、`tm_stage_sealer.py`、`tm_sqlite_store.py`、activation/recovery tests、ADR-007/008。
- **Findings**：
  - `build_mutable_stage()` 只产生 unpublished mutable stage。
  - `StageSealer` 需要私有 registry；安全 seal wrapper 也是 coordinator 私有方法。
  - `import_snapshot()/rebuild_from_snapshot()` 明确要求已有 active generation，只解决 canonical replacement。
  - 精确 `dd7c9f…` 没有把首次 build→seal→durable publication→recovery 包成公开 `MigrationOutcome` 的方法。
- **Implications**：精确 dd7 merge 后，Core 层先完成既有 Requirement 2 的 public contract：`TMMigrationService.activate_initial(source, resource_id) -> MigrationOutcome`。未通过既有 frozen/tamper/seal/publication/recovery tests 前，阻断 Controller 与当前段 TM UI；Integration/Qt 不得导入私有 sealer/registry 补洞。这是 public-contract completion，不是新增 Feature 5 产品需求或 scope amendment。

### Existing UI session、资源与偏好

- **Context**：集成必须保持唯一编辑会话、Active/Lookup/Update 与 device-local preference。
- **Sources Consulted**：`editor_contracts.py`、`editor_controller.py`、`resource_repository.py`、`workspace_state.py`、Qt suggestion/settings code。
- **Findings**：
  - 当前 TMSuggestion 只有单 source，stale 校验只比较 source。
  - resource registry 已持有稳定 order 和 Active/Lookup/Update；不应复制 canonical lifecycle flag。
  - workspace repository 已有原子 JSON persistence，可承载跨项目的 local threshold。
  - Qt 已使用 status bar 作非阻塞动作反馈；资源能力状态需要持久 inline/badge。
- **Implications**：Controller 保存当前完整 issued suggestion membership；threshold 进入 workspace；Qt 只消费 Controller contracts。

### macOS lightweight bundle

- **Context**：Finder/Dock 要显示 LocalCAT 而非 Python，同时不引入大型 packaging。
- **Sources Consulted**：现有 `qt_editor.py` Linux launcher/bootstrap、tracked silver PNG、macOS `.app` bundle layout。
- **Findings**：
  - user-local wrapper 可保存现有 Python/PySide environment和 data dir，但 shell executable `exec python` 是否保持 Dock identity 必须真实 smoke，不能仅靠 plist 推断。
  - 仓库只有 tracked silver PNG，没有 `.icns`；macOS 自带 `sips/iconutil` 可生成派生 asset。
- **Implications**：先提交/验证 derived `.icns` 和 lightweight bundle；若真实 identity 失败，回到 Design amendment讨论最小 PySide deployment。

## Architecture Pattern Evaluation

| Option | Description | Strengths | Risks / Limitations | Decision |
|--------|-------------|-----------|---------------------|----------|
| Qt direct Core calls | Qt 直接构造 store/query | 少文件 | 破坏 Layer 4、复制 authority | Reject |
| One universal Core handle | 把 legacy 伪装成 canonical handle | 表面统一 | 无 query lease、可能开放 fuzzy | Reject |
| Ports + mixed adapter | canonical service + legacy exact port | 边界清楚、可诚实降级 | 需显式 final merge | Select |
| UI post-filter threshold | 先查 60% 再过滤卡片 | 实现简单 | 降阈值丢候选、proof 不绑定 | Reject |
| New TMQuery per threshold | threshold 进入 Core query | 证明、排序和结果一致 | 每次需重查 | Select |
| Application-built activation | integration 拼私有 sealer/registry | 看似可立即实现 | 越权、破坏 capability 链 | Reject |
| Core initial activation seam | Core 内包住完整 lifecycle | authority 单一、可恢复 | 须在 dd7 merge 后补齐 Req2 公开合同并过既有测试 | Select |

## Design Decisions

### Decision：canonical cohort 与 mixed order 分离

- **Context**：Core 要求连续 cohort order，产品要求完整资源全局 order。
- **Alternatives Considered**：保留有空洞全局序号；每资源单独 query；双 order mapping。
- **Selected Approach**：canonical handles 连续编号；snapshot 另存 `global_order_by_resource_id`。
- **Rationale**：满足 Core mapping，又能稳定 interleave legacy/canonical exact。
- **Trade-offs**：adapter 需要一个有限的 exact-lane merge，但不重算 scorer/context/fuzzy。
- **Follow-up**：测试 canonical/legacy/canonical 交错和全局十条。

### Decision：capability service 原子换代

- **Context**：初始 UI 要可用 exact，而正式 expectation 只有 Gate C recomputation 后取得。
- **Alternatives Considered**：把 Gate C manifest 刷入 default publisher；阻塞启动；先 exact-only再换 service。
- **Selected Approach**：先 exact-only；后台产生 release，以其 expectation 新建 publisher/service并原子换入。
- **Rationale**：不自铸 capability，也不阻塞 Qt。
- **Trade-offs**：capability generation变化会立即使旧 suggestions stale。
- **Follow-up**：覆盖 refresh in-flight query 与下一 query 的 snapshot isolation。

### Decision：区分 Core gates 与跨 Spec checkpoints

- **Context**：Feature 5 已定义 Gate A～D 与独立 Matcher Gate；Integration 另有 merge 后 maintenance 和 handoff 后原 Qt Req3/Req7 两个跨 Spec 等待点。若都称为 Gate A/B，会隐藏范围并让 Core Gate C/D 看似可以任意插队。
- **Alternatives Considered**：沿用两套 Gate A/B/C/D；只在 Tasks 用自然语言解释；Core 保留 Gate 命名、跨 Spec 改用 Checkpoint M/Q。
- **Selected Approach**：Gate A～D 与 Matcher Gate 仅保留 Feature 5 原义；Checkpoint M 归 `qt-editor-mvp` maintenance，Checkpoint Q1/Q2 归 `qt-editor-json-mvp-increment` Req3/Req7。Steering 保存唯一八步 Critical Path，Design 自包含展开 Core gate 的范围、允许行为、失败语义与 runtime 正交状态。
- **Rationale**：同一 thread 可以跨簇执行，但 Spec/task/验收 owner 不随执行者改变；独立命名避免第二条顺序权威。
- **Trade-offs**：文档多一个 checkpoint 术语，但不再与 Core capability gate 冲突。
- **Follow-up**：Tasks 只引用 Checkpoint M/Q 和已定义的 Core gate；Task 7 未完整通过前不得进入 Checkpoint Q。

### Decision：完整 suggestion membership 校验

- **Context**：session/source/epoch 不能阻止合法形状的 target/record/similarity field substitution。
- **Alternatives Considered**：只校验 source；公开签名 token；Controller 保存 issued tuple。
- **Selected Approach**：Controller 保存当前完整 frozen tuple，apply 要求逐字段 membership。
- **Rationale**：无需新密钥/持久状态，且能拒绝所有字段替换。
- **Trade-offs**：每次 query/epoch 都替换一个最多十项的小集合。
- **Follow-up**：tamper tests逐字段替换。

## Risks & Mitigations

- Req2 `activate_initial()` public-contract completion 未通过既有合同/防篡改/seal/publication/recovery tests — 阻断 Controller 与当前段 TM UI，不从 Integration/Qt 私补。
- Gate D 实际运行昂贵 — 后台运行、exact/context 诚实可用、fuzzy 关闭状态持久可见。
- mixed merge 重述 Core 排序 — 只合并 exact lane，context/fuzzy 完整保留 Core order，并用交错 fixture验证。
- capability/resource refresh 导致旧卡片误用 — epoch + issued membership 双校验。
- lightweight `.app` 仍显示 Python — 真实 Finder/Dock gate；失败后回 Design amendment。

## References

- `.kiro/specs/tm-storage-retrieval-index/{requirements.md,design.md,tasks.md}`
- `.kiro/specs/qt-editor-json-mvp-increment/{requirements.md,design.md,tasks.md}`
- `.kiro/steering/adr/adr-007.md` ～ `adr-011.md`
- `.kiro/steering/feature5-ui-integration.md`
- Feature 5 exact source `dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b`
