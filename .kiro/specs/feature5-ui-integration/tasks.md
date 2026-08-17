# 实施计划

> 本计划只拥有 Feature 5 Core 到当前段 TM suggestions 的跨层闭环与 macOS 入口。owner 指 Spec、task checkbox、代码边界与验收权威，不指 Agent 或 thread；同一 thread 可以依次执行不同簇，但必须重新载入 owning Spec。独立 Qt maintenance、原 Qt Requirement 3 单 JSON 搜索和 Requirement 7 术语 CRUD/管理入口分别在其 owning Spec 记账，不得用本计划的 checkbox 代替。

- [x] 1. 闭合治理、精确身份与 Feature 5 合并基线

- [x] 1.1 闭合获批治理顺序与三方所有权
  - 由 Governance owner 同步已批准的八步实施顺序、Feature 5 Requirement 2 public-contract completion 和 Feature 5 Core / Integration / 原 Qt increment 三方职责
  - 保持 ADR-007～011 的既有处置与 ADR-002/006 的部分取代关系，不新增产品 scope amendment 或把实现事实冒充治理批准
  - 完成时，Steering、Spec ownership 与本 Design 对 Controller/current-segment TM UI、原 Qt Req3/Req7 和 macOS 的归属表述一致且可复查
  - _Requirements: 5.3, 7.2, 9.7_
  - _Boundary: Governance Steering and Spec Ownership_

- [x] 1.2 执行合并前精确身份与用户 WIP 门
  - 由 UI owner fail-closed 核对两个授权根、`ui-mvp`、full HEAD、来源 `feature5@dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b` 对象、可达性和工作树状态
  - 确认 UI 本地旧迁移线只保留为 `feature5-migrate@fe7afa57bfdf7ac3fc347695c304588f8ad706f2`，不得作为本次 merge 来源或补齐到 `b90de57…`
  - 对 `Demo.xlsx`、`spec.md`、`terms.csv`、`tm.jsonl` 记录 merge 前 SHA-256，并只允许显式路径暂存，不吸收、覆盖或清理其他 untracked/WIP
  - 完成时，所有身份事实与四个受保护文件 hash 和 Implementation Notes 基线一致；任一不一致均停止而不修改仓库
  - _Requirements: 5.3, 9.5, 9.7_
  - _Boundary: Repository Identity and WIP Safety Gate_

- [x] 1.3 形成精确可追踪 merge 并建立 Core 绿色基线
  - 从授权来源根引入精确 `dd7c9f…`，保留其为 merge parent，不 squash、不 cherry-pick 重建历史
  - merge 不得吸收用户 WIP；完成后再次核对 branch、full HEAD、merge parent、status 与四个受保护文件 SHA-256
  - 运行 Feature 5 frozen contracts、canonical migration/retrieval、matcher/capability 与防篡改基线，并执行 diff check
  - 完成时，merge 身份可追踪到精确 dd7，Core baseline 全绿，用户 WIP 字节未变化
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.6, 9.10_
  - _Boundary: Feature 5 Merge and Core Baseline_

> **Checkpoint M（不属于本 Spec checkbox）**：任务 1.3 完成后，切换到 `qt-editor-mvp` maintenance ledger，修复平台快捷键、下拉框对比度和无 writable termbase 时的明确操作指引。该簇完成、回归通过并独立提交后才执行任务 2.1；执行者可以是同一 thread，但本 Spec 不实现、不勾选也不验收该簇。

- [ ] 2. 补齐 Feature 5 Requirement 2 的首次激活公开合同

- [ ] 2.1 冻结首次激活入口、身份前置与私有边界
  - 提供唯一 application-facing 首次激活入口，精确绑定 configured source、resource identity 与 Core-owned coordinator
  - 只接受没有 active generation 的首次激活；already-active、foreign identity 或无效前置返回稳定结果并保持零修改
  - private registry、mutable/sealed stage、prepared activation 与 capability token 不得暴露给 application、Controller 或 Qt
  - 完成时，公开合同、非法组合和 already-active 的 frozen/zero-mutation tests 全部通过
  - _Requirements: 5.2, 5.3, 5.5, 5.8_
  - _Boundary: Feature 5 Initial Activation Contract_

- [ ] 2.2 闭合首 generation 的完整发布与重开
  - 在 Core 内完成 build、seal、durable publication、active verification 与 runtime reopen 的单一首次激活事务
  - 只有完整验证的 generation 才成为 canonical authority；context/fuzzy 仍分别受正式 capability gate 约束
  - 完成时，成功 outcome 指向唯一首 generation，重开后可通过正式 store/retrieval port 查询 canonical exact，publication tests 全绿
  - _Requirements: 5.7, 5.8_
  - _Boundary: Feature 5 Initial Activation Publication_

- [ ] 2.3 证明未发布与完整回滚时保留 legacy
  - 用户取消只发生在进入 Core transaction 之前；正式调用后不接受 UI cancellation token
  - 已证明从未发布 canonical authority 或已完整 rollback 时，保持原 JSONL 字节、资源配置与 legacy exact-only 能力
  - 不允许部分 stage、journal 或配置变化伪装为可继续使用的 legacy 状态
  - 完成时，pre-call cancel、build/seal/publish 前失败和完整 rollback 的 byte-hash/authority tests 全部通过
  - _Requirements: 5.4, 5.6_
  - _Boundary: Feature 5 Initial Activation Rollback_

- [ ] 2.4 闭合发布尾部恢复与不明确持久事实
  - 若 `GENERATION_PUBLISHED` 已完整闭合但尾部返回异常，恢复并返回同一 generation，不重复迁移或生成第二权威
  - pending journal、rollback/recovery 无法证明或 durable facts 不明确时进入 `UNAVAILABLE`，不得回落 legacy exact-only
  - 完成时，published-tail、pending/ambiguous journal 与 recovery 重启矩阵得到稳定、互斥 outcome
  - _Requirements: 5.6, 5.10, 6.4_
  - _Boundary: Feature 5 Activation Recovery_

- [ ] 2.5 验证激活防篡改、并发与 canonical 更新保全
  - 覆盖 identity tamper、foreign resource、并发首次激活、stage/manifest/content 变化和不可证明 cleanup
  - 已有 canonical 资源的显式更新继续走既有路径；失败保留 last-known-good generation，不查询 JSONL 替代
  - 完成时，全部对抗性用例返回稳定 failure code、无重复 generation、无部分 authority，LKG 可继续重开查询
  - _Requirements: 5.9, 5.10, 6.4_
  - _Boundary: Feature 5 Activation Tamper and Update Regression_

- [ ] 3. 建立冻结 UI 合同、偏好与 capability composition

- [ ] 3.1 升级 TM suggestion、状态与查询身份的冻结合同
  - 建议投影保留 resource/record identity、query source、matched source、target、Core match type、final similarity 与安全 provenance
  - 查询身份完整绑定 project session、segment/source、resource/capability/threshold epoch，并允许 Controller 保存完整 issued membership
  - 状态与失败投影只表达 safe codes；不得携带 raw evidence、candidate proof、折叠文本或中间评分
  - 完成时，type/range/source relationship、roundtrip 与逐字段 tamper tests 全部通过
  - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 4.1, 4.2, 4.3, 4.4, 5.1, 6.1_
  - _Boundary: Editor TM Frozen Contracts_

- [ ] 3.2 (P) 持久化 device-local fuzzy 阈值
  - 默认值为 0.60，只接受有限的 0.60～1.00 闭区间；非法更新保留旧值，损坏或旧版本状态回退 0.60
  - 阈值跨项目与重启共享，但不写入项目、TM、术语表或网络位置；首版结果上限固定为 10
  - 使用现有原子本地状态语义，持久化失败不产生新值或部分文件
  - 完成时，边界、非法输入、重启、项目切换和 byte-location tests 全部通过
  - _Requirements: 3.1, 3.2, 3.3, 3.8, 3.9, 3.10, 9.5_
  - _Boundary: WorkspaceState_
  - _Depends: 3.1_

- [ ] 3.3 (P) 建立 exact-only 的不可变 runtime host
  - 启动时使用 fail-closed sentinel publisher，仅提供安全 exact 能力，不以 store health、调用方布尔值或局部 PASS 开放高级检索
  - 每个 query 捕获一次 immutable matcher/retrieval capability snapshot，在途操作不混用刷新后的 generation；资源 snapshot 生命周期仍由 resolver 与 resource lifecycle 任务拥有
  - matcher 与 retrieval capability 分别保持 Core authority；`degraded` 只作为单向 UI display projection
  - 完成时，closed 初态、单 snapshot 与 in-flight isolation tests 全部通过
  - _Requirements: 5.8, 6.1, 6.2, 6.3, 6.4_
  - _Boundary: CapabilityHost_
  - _Depends: 3.1_

- [ ] 3.4 装配独立 Matcher Gate 与中立 TextMatcher
  - 只消费 Core matcher validation manifest，并且只由 Core validated matcher factory 构造 matcher；发布 UNAVAILABLE、BASIC_VALIDATED 或 TEXT_V1_VALIDATED 的 immutable snapshot，UNAVAILABLE 时 matcher 必须为 `None`
  - BASIC 只允许基础连续搜索；TEXT_V1 才允许 Match Case、Whole Word 与 configured terms，纯 CJK 复用连续文本语义
  - 不得从 SQLite、retrieval Gate C/D、FTS5、控件状态或调用方布尔值推断 matcher state
  - refresh 原子替换 matcher snapshot、递增 matcher generation，并通知 Controller 使旧 matcher 相关状态失效；在途操作继续使用已捕获 snapshot
  - 完成时，manifest missing/expired/foreign、三态转换、factory binding、shared Unicode/CJK vectors、generation invalidation 与 single-snapshot tests 全部通过
  - _Requirements: 7.1, 7.2, 7.3, 7.4_
  - _Boundary: CapabilityHost Matcher Gate_

- [ ] 3.5 以 Gate C 配对结果原子换入正式 retrieval service
  - validation recomputation 必须同时产生配对的 expectation 与 manifest，并用该 expectation 新建 evaluator、publisher 与 service
  - 不得把批准 manifest 刷入 sentinel default publisher；重算、构造或刷新失败时保持当前较低能力
  - Gate C 成功最多独立开放 CONTEXT；fuzzy-core 只满足 correctness 前提，FUZZY 必须继续关闭直至本次 intended path 的 Gate D 通过
  - 成功换入后递增 capability generation，使下一次查询使用新 snapshot，旧在途查询继续完成
  - 完成时，paired/foreign/expired manifest、atomic swap 与 refresh isolation tests 全部通过
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.7_
  - _Boundary: CapabilityHost Gate C_

- [ ] 3.6 在同一 publisher 上闭合 Gate D 运行生命周期
  - 只使用合并后已跟踪的 `benchmark_tm_contract.json`，并为每个 process/code epoch 创建新的 `0700` private work root
  - evidence path 在调用前必须不存在；旧 evidence 或 receipt 不得在后续进程重铸授权
  - 只有配对的 Gate C fuzzy-core 与本次 intended-path Gate D 同时通过才开放 FUZZY；失败保留 canonical exact 与已开放 context，不得提升 fuzzy
  - Gate D 在后台运行，不阻塞 Qt；`GATE_D.CLEANUP_PENDING` 或 identity drift 时保留现场并保持 closed，application 不递归清理或推断通过
  - 完成时，Gate D success、old receipt、absent evidence、cleanup pending、identity drift、非阻塞与 exact/context preservation tests 全部通过
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.7_
  - _Boundary: CapabilityHost Gate D_

- [ ] 3.7 将 declarative 资源解析为有序 runtime ports
  - 根据 Active/Lookup/Update 与显式资源顺序构造不可变 snapshot，不把 canonical lifecycle flag 复制进 registry
  - canonical cohort 使用连续 Core order，另保留完整 global resource order；legacy 与 canonical 获得各自正式 port
  - resolver 只负责 open-time identity、ports 与 snapshot，不承载 query、scorer、排序或写回规则
  - 完成时，legacy/canonical/legacy 交错配置可确定性重建相同 ports、orders 与权限集合
  - _Requirements: 1.1, 2.1, 2.7, 4.6, 4.7_
  - _Boundary: TMResourceResolver Configuration and Ports_

- [ ] 3.8 闭合 lifecycle 分类、局部失败与旧 snapshot 生命周期
  - 依据 Core activation facts 将资源分类为 legacy exact-only、canonical active、source-diverged 或 unavailable
  - path/open/query-lease 前置失败只生成 resource-local safe status；已激活 authority 无法证明时不回落 JSONL
  - 新 snapshot 完整构造后一次替换；在途查询持有旧 snapshot 直到结束，不读取半刷新集合
  - 完成时，缺失路径、损坏 activation facts、divergence、atomic replacement 与 in-flight lifetime tests 全部通过
  - _Requirements: 5.1, 5.9, 5.10, 6.5, 6.6, 6.7_
  - _Boundary: TMResourceResolver Lifecycle and Failure_

- [ ] 4. 实现 current-segment mixed retrieval adapter

- [ ] 4.1 映射 canonical current-segment 查询并消费 production retrieval
  - 使用 raw 当前 source 作为 query source、raw speaker 作为 speaker identity；没有正式 context 时传 `None`，不擅自把相邻 UI 段当 Core context
  - 每次阈值变化构造新的查询，传入当前 minimum similarity 与固定 limit 10
  - 原样消费 production `TMRetrievalService` 的 match type、similarity、matched source 与稳定顺序，不重算评分或证明
  - 完成时，exact/context/fuzzy、0.60 inclusive、低于阈值和 1.00 fuzzy 的 adapter tests 使用 Core 结果通过
  - _Requirements: 1.1, 1.4, 1.5, 3.4, 3.5, 3.6, 3.7, 3.10, 9.7_
  - _Boundary: EditorTMAdapter Canonical Query_

- [ ] 4.2 实现 query-time legacy exact compatibility port
  - 保持 source-LWW、direct exact 优先和严格 same-speaker Ren'Py alias；无法安全解包时拒绝兼容命中
  - legacy 只产生 exact，不能因 mixed canonical 资源存在而获得 context/fuzzy
  - legacy path/read 失败只返回该资源 safe failure，不吞掉 canonical 成功结果
  - 完成时，direct/alias/unsafe alias/LWW/local failure tests 均保持 raw speaker identity 与 exact-only
  - _Requirements: 2.1, 2.7, 6.5, 6.6, 9.1, 9.2, 9.6_
  - _Boundary: EditorTMAdapter Legacy Query_

- [ ] 4.3 聚合 mixed 结果并输出安全 UI projection
  - 合并 legacy 与 canonical exact lane，随后保持 canonical context/fuzzy 的 Core order
  - 以 resource/record 或稳定 legacy identity 去重，在全部资源汇总后只应用一次 global top-10
  - 将 Core report 映射为 frozen suggestion、capability 与 resource status，区分真正 no-match 和 failure/degraded
  - 完成时，跨资源 exact-first、fuzzy score、ties、重复查询、双 source 和全局十条测试结果稳定
  - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 2.2, 2.3, 2.4, 2.5, 2.6, 6.5, 6.6, 9.8_
  - _Boundary: EditorTMAdapter Mixed Aggregation and Projection_

- [ ] 4.4 建立确认译文的 TM append port
  - 只遍历 snapshot 中 Active+Update 的 TM ports，分别调用 canonical 与 legacy 的正式 append 能力
  - 返回 per-resource structured write report；无 writable TM 时沿用既有确认语义，不制造虚假写入
  - Update=false 资源不进入写路径，资源失败不被折叠为普通成功
  - 完成时，canonical/legacy/mixed/no-writable/partial-failure 与 Update=false byte-hash tests 全部通过
  - _Requirements: 4.6, 4.7, 9.1_
  - _Boundary: EditorTMAdapter Confirmed Append_

- [ ] 5. 在 EditorController 闭合查询、应用、确认与激活

- [ ] 5.1 建立 current query epoch 与 issued suggestion membership
  - project/session、segment/source、resource snapshot、capability snapshot 或 threshold 变化时递增 epoch 并清空旧建议集合
  - 每次当前段查询只使用一次完整 runtime snapshot，原子保存本次最多十条完整 frozen suggestion tuple
  - 状态恢复或资源刷新后自动重新查询当前段；相同状态重复查询保持同集合与顺序
  - 完成时，所有 epoch trigger、in-flight refresh 与重复查询 tests 都能确定旧卡是否 stale
  - _Requirements: 1.1, 2.6, 3.4, 4.3, 6.7_
  - _Boundary: EditorController TM Query Session_

- [ ] 5.2 拒绝 stale/tampered suggestion 并只应用目标译文
  - EXACT、CONTEXT 与 FUZZY 一律要求用户显式 apply，不允许自动写 target
  - 校验完整 issued membership，拒绝合法形状下替换 resource、record、target、type、score 或 provenance 的 field substitution
  - 成功只更新当前 target 并保持未确认/dirty 语义，不写 TM、不确认、不跳段；失败保持所有项目与 TM 状态不变
  - 完成时，三种类型成功路径和逐字段 tamper/stale zero-mutation tests 全部通过
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - _Boundary: EditorController TM Suggestion Apply_

- [ ] 5.3 以 structured write report 协调确认与导航
  - 确认当前段时调用 adapter append port，并把每个资源的成功或失败反馈给上层
  - required write 失败时不确认、不导航；Update=false 前后资源 hash 不变
  - 没有 writable TM 时保持既有可确认行为，避免把配置选择变成隐式数据写入
  - 完成时，success/partial failure/no-writable/Update matrix 中 confirmed、dirty、当前位置与资源字节符合设计
  - _Requirements: 4.4, 4.6, 4.7, 9.1_
  - _Boundary: EditorController Confirmed TM Write_

- [ ] 5.4 提供首次激活的 preflight、确认与 operation lifecycle
  - 首次激活先返回只读 preflight，显示目标资源、source 与预期状态变化；开始前可取消且零修改
  - 正式开始后创建安全 operation id 与 display phase，禁用重复激活和取消，不向 Qt 暴露 stage/path/token
  - 后台 worker 只调用 Core public contract，其他不冲突的编辑功能继续可用
  - 完成时，preflight/cancel/start/busy/duplicate tests 返回稳定 Controller state
  - _Requirements: 5.2, 5.3, 5.4, 5.5_
  - _Boundary: EditorController Activation Start_

- [ ] 5.5 在 activation completion 后重建并原子替换 runtime
  - 成功 outcome 后重新 resolve、re-prove 并一次替换 resource snapshot，再递增 epoch、刷新状态与当前建议
  - proven first failure 保留 legacy；ambiguous facts 显示 unavailable；canonical update 失败保留 LKG 与 source-diverged 状态
  - completion 或 resolver 失败不得发布半套 capability/resource 集合
  - 完成时，success/proven failure/ambiguous/diverged/LKG 的 Controller integration tests 全部通过
  - _Requirements: 5.6, 5.7, 5.8, 5.9, 5.10, 6.7_
  - _Boundary: EditorController Activation Completion_

- [ ] 5.6 闭合阈值更新、持久化与重新查询
  - Controller 统一校验并保存阈值，成功后递增 epoch、构造新查询并刷新当前建议
  - 非有限、越界或持久化失败保留旧值、旧 epoch 与当前结果，并返回可理解的 non-blocking outcome
  - 两个 UI 入口只消费同一 Controller 状态，不各自保存第二份 preference
  - 完成时，0.60/1.00/非法/持久化失败/重启/项目切换 tests 全部通过
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 7.7_
  - _Boundary: EditorController TM Threshold_

- [ ] 6. 构建本 Integration 拥有的 Qt TM surfaces

- [ ] 6.1 (P) 升级当前段 TM suggestion cards
  - 显示 EXACT/CONTEXT/FUZZY、百分比、matched source、target 与 resource；query source 相同时避免无意义重复，fuzzy 时明确实际命中原文
  - 每条卡片提供显式 apply；成功或拒绝沿用 status bar 等非阻塞反馈
  - 将 no match 与 capability/resource failure 分开呈现，不把失败伪装成“暂无建议”
  - 完成时，offscreen cards、三类显示、双 source、apply 与 no-match/failure journeys 全部通过
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 4.1, 4.2, 4.3, 4.4, 4.5, 6.5, 6.6_
  - _Boundary: Qt TM Suggestion Surface_
  - _Depends: 5.1, 5.2_

- [ ] 6.2 (P) 在语言资源设置呈现 canonical lifecycle 与操作
  - 每个 TM 资源持续显示 legacy exact-only、canonical active、source-diverged、degraded 或 unavailable 及有限 safe reason
  - 提供显式 activate/rebuild 动作；busy 时禁用重复操作，打开设置、刷新或查询不得触发迁移
  - 单资源失败保持其他资源状态和成功结果，未知内部异常不展示正文、path 或 proof body
  - 完成时，状态、preflight、busy、success/failure/rebuild 与 no-startup-migration Qt tests 全部通过
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_
  - _Boundary: Qt Settings TM Status_
  - _Depends: 5.4, 5.5_

- [ ] 6.3 打通两个一致、可发现的 fuzzy 阈值入口
  - Translation Matches 区提供始终可发现、可聚焦且不依赖 hover 的紧凑阈值 chip
  - 语言资源设置的 TM section 提供第二入口；两个入口显示同一有效值、capability state 与 disabled reason
  - 成功或失败用 status bar 等非阻塞反馈；不新增仅靠鼠标悬停才能操作的入口
  - 完成时，Tab/Enter/Space、同步更新、fuzzy unavailable、重启与项目切换 Qt tests 全部通过
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.8, 7.5, 7.6, 7.7_
  - _Boundary: Qt TM Threshold Integration_
  - _Depends: 5.6, 6.1, 6.2_

- [ ] 6.4 收紧 Layer 4、可访问性与 offscreen 边界
  - Qt 只依赖 `EditorController` 与 frozen contracts；AST guard 禁止 store、retrieval、evaluator、proof 或 migration implementation 越层导入
  - 所有新增控件提供稳定 object name、accessible name、tooltip、Tab focus 和 Enter/Space 操作
  - persistent capability/resource state 与 transient action feedback 分工清楚，不用 disabled 空壳冒充能力已完成
  - 完成时，AST、accessibility、keyboard 与 offscreen boundary tests 全部通过
  - _Requirements: 1.7, 6.4, 7.5, 7.6, 7.7, 9.4, 9.5_
  - _Boundary: Qt Layer 4 Boundary and Accessibility_

- [ ] 7. 完成 TextMatcher handoff 与 canonical integration 验收

- [ ] 7.1 向原 Qt Spec 交付唯一中立 TextMatcher handoff
  - composition root 向原 Qt Requirement 3/7 提供唯一 matcher port 与安全 capability projection，不提供本地 casefold/Whole Word/CJK fallback
  - 重验 BASIC/TEXT_V1、Unicode、数字/下划线/标点、纯 CJK shared vectors 和 legacy Trie 语义
  - 本任务不实现项目搜索控件或术语 CRUD，也不勾选原 Qt tasks
  - 完成时，handoff contract tests 证明移除 Core matcher 会 fail closed，原 Qt 产品任务仍保持原 owner
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 9.2, 9.4_
  - _Boundary: TextMatcher Application Handoff_
  - _Depends: 3.4_

- [ ] 7.2 建立并重开真实 activated SQLite 验收 fixture
  - 通过 production migration/activation 生成 canonical SQLite，不直接手写伪 store 或用 legacy importer 代替
  - fixture 包含 same-source multi-target、distinct matched source、非 100%、0.60 边界、低于阈值、1.00 fuzzy 与跨资源 ties
  - 重开后验证 generation identity、record variants 和正式 query lease 仍有效
  - 完成时，fixture 可重复构建/重开且 legacy 100% 卡片不参与 canonical 通过依据
  - _Requirements: 9.6, 9.7, 9.8_
  - _Boundary: Canonical TM Integration Fixtures_

- [ ] 7.3 验证 canonical 阈值、排序与 mixed global top-10
  - 使用 production retrieval 覆盖 exact/context/fuzzy、双 source、最终分数和 0.60 inclusive / 1.00 fuzzy
  - 交错 legacy/canonical 资源，验证 exact-first、相似度降序、Core ties、去重和全资源一次 top-10
  - 相同 snapshot 重复查询返回相同集合与顺序，阈值降低会通过新查询补回候选
  - 完成时，所有结果可追溯到真实 activated fixture 与 Core report，不存在 UI 后过滤或重排
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 3.4, 3.5, 3.6, 3.7, 9.1, 9.7, 9.8_
  - _Boundary: Canonical and Mixed Retrieval Validation_
  - _Depends: 3.5, 3.6_

- [ ] 7.4 验证 capability、资源与 activation 失败矩阵
  - 覆盖 context/fuzzy closed、expired、foreign evidence、Gate C/D 失败与状态恢复
  - 覆盖 legacy/canonical path、query、reopen 局部失败，proven first failure、ambiguous activation、source divergence 与 LKG
  - 断言失败持续可见、其他资源结果保留、activated authority 不回落 JSONL
  - 完成时，每个 failure code 对应稳定安全投影且没有被渲染为普通 no-match
  - _Requirements: 5.6, 5.8, 5.9, 5.10, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 9.9_
  - _Boundary: Capability Resource and Activation Failure Validation_
  - _Depends: 3.5, 3.6_

- [ ] 7.5 验证 stale/tamper/apply 与写回权限矩阵
  - 对 project/segment/source/resource/capability/threshold epoch 变化和逐字段 suggestion substitution 执行 zero-mutation tests
  - 覆盖三种 match type 的显式 apply，以及 Active+Lookup 与 Active+Update 的 canonical/legacy 组合
  - 对 Update=false 资源比较操作前后 SHA-256，并验证 partial write failure 不确认、不导航
  - 完成时，所有 stale/tamper/permission 路径保持 target、confirmed、dirty、位置与未授权资源字节不变
  - _Requirements: 4.3, 4.4, 4.5, 4.6, 4.7, 9.9_
  - _Boundary: TM Suggestion and Write Permission Validation_

- [ ] 7.6 执行 Qt、legacy、Excel 与本地性回归
  - 运行 offscreen journeys、exact priority、raw speaker/Ren'Py、Trie、JSON/TXT/save/confirmed/dirty/resource switches 与 Excel 三态
  - 验证 DTD/ENTITY TMX 仍被拒绝且目标字节不变，项目、TM、术语、阈值和诊断不离开本机
  - 对 changed files 运行 `basedpyright --level error`，并执行 `git diff --check` 与四个用户 WIP SHA-256 复核
  - 完成时，完整 integration baseline 在当前提交全绿且没有用 legacy 结果替代 canonical 验收
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.10_
  - _Boundary: Integration Regression Validation_

> **Checkpoint Q（不属于本 Spec checkbox）**：任务 7 的全部 handoff 与 validation 子任务闭合后，暂停本 Spec。先按原 `qt-editor-json-mvp-increment` 完成 Q1 / Requirement 3 单 JSON 搜索，再完成 Q2 / Requirement 7 术语 CRUD 与管理入口，并只更新原 Spec 的 Tasks。执行者可以是同一 thread；两簇以 fresh evidence 完成后才返回任务 8。

- [ ] 8. 实现、验收并独立提交 macOS `LocalCAT.app`
  - 在临时 sibling 中完整生成并验证 user-local lightweight bundle 后原子替换目标，失败保留旧 bundle
  - 固定 `LocalCAT` name/display name、稳定 bundle identifier、silver `.icns` 与 `LocalCAT` executable；使用经验证的绝对 Python/bootstrap 路径且不依赖 Finder 工作目录
  - 真实 Finder/Dock cold launch 显示 LocalCAT 而非 Python，使用同一 data dir、资源配置、项目与 TM suggestions
  - 路径或环境失效时明确非零失败；Linux launcher 与 `python qt_editor.py --sample` 不回归
  - 若 lightweight 入口不能保持真实 identity，停止本任务并回到 Design amendment，不自动引入大型 packaging 依赖
  - 完成时，bundle metadata、icon/name、cold launch、cwd independence、failure 与 CLI/Linux smoke 全部通过，并形成独立 macOS commit
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 9.5_
  - _Boundary: MacOSAppLauncher and Bundle Identity_

- [ ] 9. 闭合实现后 Steering 与 Feature GO

- [ ] 9.1 同步实际结构与技术事实
  - 由 Governance owner 只按 Design 指定更新 `product.md`、`tech.md`、`structure.md` 的 canonical runtime、composition root 与新增文件事实
  - 对已同步的 integration boundary、roadmap 与 ownership 只做一致性复核；没有 owner-approved delta 时不例行重写
  - 由 UI owner记录原 Qt Req3/Req7 downstream revalidation、Parser/multi-document triggers 与各 cluster/commit ownership，不把相邻工作改记到本 Spec
  - 完成时，Steering、实际 tree、runtime 与测试事实一致，ADR 取代关系未被静默改写
  - _Requirements: 7.2, 9.4, 9.5_
  - _Boundary: Post-Implementation Steering Sync_
  - _Depends: 8_

- [ ] 9.2 以当前提交 fresh evidence 执行 Feature GO
  - 确认原 Qt Requirement 3/7 已在原 Spec 完成并提供 fresh evidence；本 Spec 不复制或勾选其 checkbox
  - 实际重验 Requirement 7.1～7.4 的 matcher capability、Match Case/Whole Word、CJK 与 legacy Trie 产品行为
  - 重新运行完整 Core frozen/tamper/matcher/capability/retrieval、canonical/Qt journeys、Excel、CLI/Linux/macOS、changed-file basedpyright 与 diff check
  - 再次核对四个用户 WIP SHA-256，确认无未决治理、无未批准 packaging、无跨 Spec 任务混记；任一门失败则拒绝 GO
  - 完成时，所有门在当前提交绿色且证据直接验证目标业务 API 与 Finder/Dock 入口
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7, 9.8, 9.9, 9.10_
  - _Boundary: Feature GO and Cross-Spec Revalidation_

## Implementation Notes

- 用户 WIP 基线：`Demo.xlsx de4d85b4dc8ce2e828dea4b2941ad0748f937b307df61d8a3d98f454bbb2bb7f`；`spec.md d781dc2d324b69199d3078ee485a2ca224a9f18c5946f7712c8874af3719b611`；`terms.csv 36ec5fca0895fd0e4f1229a2650b9b5dfe2e3aa87599caeda67c04c68860a837`；`tm.jsonl 82b1597aba42dcc40bcd9404485ed9a1140103713af7872ea5eae1619c1e4f73`。
- 合并前身份基线：授权 UI 根为 `ui-mvp@af23b2a534f3ff061d033470e3112ede309720cc`；授权 Feature 5 source 根为 `feature5@dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b`；两 tip 的 merge-base 与各自相对的历史共同基线均为 `459b524e72ce3d1f3925088669988a0e730cdb39`；UI object database 尚未引入 `dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b`；UI 本地旧迁移 ref 为 `feature5-migrate@fe7afa57bfdf7ac3fc347695c304588f8ad706f2`，不得用于精确 merge 或补齐到 `b90de57…`。
- canonical 红线：legacy importer 的 source-LWW 与当前 100% 卡片只验 exact compatibility；多译文、非 100%、阈值、排序和 fuzzy 必须使用真实 activated SQLite 与 production retrieval API。
- 每个实现子任务经独立 review 与 fresh completion evidence 后，使用显式路径暂存该任务文件和本 `tasks.md` checkbox；不得 `git add -A`、`git add .`、stash 或吸收用户 WIP。
- cluster review 是逐任务门后的额外退出证据：按 `.kiro/steering/feature5-ui-integration-review-clustering.md` 记录 full base/tip、累计 diff 与共享故障矩阵；Checkpoint M/Q 仍只更新 owning Spec，不得在本文件代勾。
- 2026-08-17 / Task 1.3：以 merge commit `482dd5b` 保留精确 `dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b` 为第二父；唯一 roadmap 冲突保留 Integration contract 与 Core gate 事实，acceptance/release evidence 由官方工具绑定 merged UI source（33/33、86/86 GO），canonical suite 1629 tests 全绿并保留 1 个明确 opt-in 100k envelope skip；四个用户 WIP SHA-256 未变化。
- 2026-08-17 / G0 cluster：native reviewer 对 `105449a5838763e0a08a05a600a45a457d589edf..9479c4f33d2c540fb300b0773d335dec3ffc7f24` 累计补丁复审通过；fresh cluster suite 在 reviewed tip 运行 1629 tests / 413.467s，Qt smoke、FTS5/fallback 5k/200 oracle 与 release 86/86 GO 全绿，唯一 skip 仍是明确 opt-in 100k migration envelope；WIP hashes 未变化。
