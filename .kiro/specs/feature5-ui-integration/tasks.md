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

- [x] 2. 补齐 Feature 5 Requirement 2 的首次激活公开合同

- [x] 2.1 冻结首次激活入口、身份前置与私有边界
  - 提供唯一 application-facing 首次激活入口，精确绑定 configured source、resource identity 与 Core-owned coordinator
  - 只接受没有 active generation 的首次激活；already-active、foreign identity 或无效前置返回稳定结果并保持零修改
  - private registry、mutable/sealed stage、prepared activation 与 capability token 不得暴露给 application、Controller 或 Qt
  - 完成时，公开合同、非法组合和 already-active 的 frozen/zero-mutation tests 全部通过
  - _Requirements: 5.2, 5.3, 5.5, 5.8_
  - _Boundary: Feature 5 Initial Activation Contract_

- [x] 2.2 闭合首 generation 的完整发布与重开
  - 在 Core 内完成 build、seal、durable publication、active verification 与 runtime reopen 的单一首次激活事务
  - 只有完整验证的 generation 才成为 canonical authority；context/fuzzy 仍分别受正式 capability gate 约束
  - 完成时，成功 outcome 指向唯一首 generation，重开后可通过正式 store/retrieval port 查询 canonical exact，publication tests 全绿
  - _Requirements: 5.7, 5.8_
  - _Boundary: Feature 5 Initial Activation Publication_

- [x] 2.3 证明未发布与完整回滚时保留 legacy
  - 用户取消只发生在进入 Core transaction 之前；正式调用后不接受 UI cancellation token
  - 已证明从未发布 canonical authority 或已完整 rollback 时，保持原 JSONL 字节、资源配置与 legacy exact-only 能力
  - 不允许部分 stage、journal 或配置变化伪装为可继续使用的 legacy 状态
  - 完成时，pre-call cancel、build/seal/publish 前失败和完整 rollback 的 byte-hash/authority tests 全部通过
  - _Requirements: 5.4, 5.6_
  - _Boundary: Feature 5 Initial Activation Rollback_

- [x] 2.4 闭合发布尾部恢复与不明确持久事实
  - 若 `GENERATION_PUBLISHED` 已完整闭合但尾部返回异常，恢复并返回同一 generation，不重复迁移或生成第二权威
  - pending journal、rollback/recovery 无法证明或 durable facts 不明确时进入 `UNAVAILABLE`，不得回落 legacy exact-only
  - 完成时，published-tail、pending/ambiguous journal 与 recovery 重启矩阵得到稳定、互斥 outcome
  - _Requirements: 5.6, 5.10, 6.4_
  - _Boundary: Feature 5 Activation Recovery_

- [x] 2.5 验证激活防篡改、并发与 canonical 更新保全
  - 覆盖 identity tamper、foreign resource、并发首次激活、stage/manifest/content 变化和不可证明 cleanup
  - 已有 canonical 资源的显式更新继续走既有路径；失败保留 last-known-good generation，不查询 JSONL 替代
  - 完成时，全部对抗性用例返回稳定 failure code、无重复 generation、无部分 authority，LKG 可继续重开查询
  - _Requirements: 5.9, 5.10, 6.4_
  - _Boundary: Feature 5 Activation Tamper and Update Regression_

- [ ] 3. 建立冻结 UI 合同、偏好与 capability composition

- [x] 3.1 升级 TM suggestion、状态与查询身份的冻结合同
  - 建议投影保留 resource/record identity、query source、matched source、target、Core match type、final similarity 与安全 provenance
  - 查询身份完整绑定 project session、segment/source、resource/capability/threshold epoch，并允许 Controller 保存完整 issued membership
  - 状态与失败投影只表达 safe codes；不得携带 raw evidence、candidate proof、折叠文本或中间评分
  - 完成时，type/range/source relationship、roundtrip 与逐字段 tamper tests 全部通过
  - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 4.1, 4.2, 4.3, 4.4, 5.1, 6.1_
  - _Boundary: Editor TM Frozen Contracts_

- [x] 3.2 (P) 持久化 device-local fuzzy 阈值
  - 默认值为 0.60，只接受有限的 0.60～1.00 闭区间；非法更新保留旧值，损坏或旧版本状态回退 0.60
  - 阈值跨项目与重启共享，但不写入项目、TM、术语表或网络位置；首版结果上限固定为 10
  - 使用现有原子本地状态语义，持久化失败不产生新值或部分文件
  - 完成时，边界、非法输入、重启、项目切换和 byte-location tests 全部通过
  - _Requirements: 3.1, 3.2, 3.3, 3.8, 3.9, 3.10, 9.5_
  - _Boundary: WorkspaceState_
  - _Depends: 3.1_

- [x] 3.3 (P) 建立 exact-only 的不可变 runtime host
  - 启动时使用 fail-closed sentinel publisher，仅提供安全 exact 能力，不以 store health、调用方布尔值或局部 PASS 开放高级检索
  - 每个 query 捕获一次 immutable matcher/retrieval capability snapshot，在途操作不混用刷新后的 generation；资源 snapshot 生命周期仍由 resolver 与 resource lifecycle 任务拥有
  - matcher 与 retrieval capability 分别保持 Core authority；`degraded` 只作为单向 UI display projection
  - 完成时，closed 初态、单 snapshot 与 in-flight isolation tests 全部通过
  - _Requirements: 5.8, 6.1, 6.2, 6.3, 6.4_
  - _Boundary: CapabilityHost_
  - _Depends: 3.1_

- [x] 3.4 装配独立 Matcher Gate 与中立 TextMatcher
  - 只消费 Core matcher validation manifest，并且只由 Core validated matcher factory 构造 matcher；发布 UNAVAILABLE、BASIC_VALIDATED 或 TEXT_V1_VALIDATED 的 immutable snapshot，UNAVAILABLE 时 matcher 必须为 `None`
  - BASIC 只允许基础连续搜索；TEXT_V1 才允许 Match Case、Whole Word 与 configured terms，纯 CJK 复用连续文本语义
  - 不得从 SQLite、retrieval Gate C/D、FTS5、控件状态或调用方布尔值推断 matcher state
  - refresh 原子替换 matcher snapshot、递增 matcher generation，并通知 Controller 使旧 matcher 相关状态失效；在途操作继续使用已捕获 snapshot
  - 完成时，manifest missing/expired/foreign、三态转换、factory binding、shared Unicode/CJK vectors、generation invalidation 与 single-snapshot tests 全部通过
  - _Requirements: 7.1, 7.2, 7.3, 7.4_
  - _Boundary: CapabilityHost Matcher Gate_

- [x] 3.5 以 Gate C 配对结果原子换入正式 retrieval service
  - validation recomputation 必须同时产生配对的 expectation 与 manifest，并用该 expectation 新建 evaluator、publisher 与 service
  - 不得把批准 manifest 刷入 sentinel default publisher；重算、构造或刷新失败时保持当前较低能力
  - Gate C 成功最多独立开放 CONTEXT；fuzzy-core 只满足 correctness 前提，FUZZY 必须继续关闭直至本次 intended path 的 Gate D 通过
  - 成功换入后递增 capability generation，使下一次查询使用新 snapshot，旧在途查询继续完成
  - 完成时，paired/foreign/expired manifest、atomic swap 与 refresh isolation tests 全部通过
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.7_
  - _Boundary: CapabilityHost Gate C_

- [x] 3.6 在同一 publisher 上闭合 Gate D 运行生命周期
  - 只使用合并后已跟踪的 `benchmark_tm_contract.json`，并为每个 process/code epoch 创建新的 `0700` private work root
  - evidence path 在调用前必须不存在；旧 evidence 或 receipt 不得在后续进程重铸授权
  - 只有配对的 Gate C fuzzy-core 与本次 intended-path Gate D 同时通过才开放 FUZZY；失败保留 canonical exact 与已开放 context，不得提升 fuzzy
  - Gate D 在后台运行，不阻塞 Qt；`GATE_D.CLEANUP_PENDING` 或 identity drift 时保留现场并保持 closed，application 不递归清理或推断通过
  - 完成时，Gate D success、old receipt、absent evidence、cleanup pending、identity drift、非阻塞与 exact/context preservation tests 全部通过
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.7_
  - _Boundary: CapabilityHost Gate D_

- [x] 3.7 将 declarative 资源解析为有序 runtime ports
  - 根据 Active/Lookup/Update 与显式资源顺序构造不可变 snapshot，不把 canonical lifecycle flag 复制进 registry
  - canonical cohort 使用连续 Core order，另保留完整 global resource order；legacy 与 canonical 获得各自正式 port
  - resolver 只负责 open-time identity、ports 与 snapshot，不承载 query、scorer、排序或写回规则
  - 完成时，legacy/canonical/legacy 交错配置可确定性重建相同 ports、orders 与权限集合
  - _Requirements: 1.1, 2.1, 2.7, 4.6, 4.7_
  - _Boundary: TMResourceResolver Configuration and Ports_

- [x] 3.8 闭合 lifecycle 分类、局部失败与旧 snapshot 生命周期
  - 依据 Core activation facts 将资源分类为 legacy exact-only、canonical active、source-diverged 或 unavailable
  - path/open/query-lease 前置失败只生成 resource-local safe status；已激活 authority 无法证明时不回落 JSONL
  - 新 snapshot 完整构造后一次替换；在途查询持有旧 snapshot 直到结束，不读取半刷新集合
  - 完成时，缺失路径、损坏 activation facts、divergence、atomic replacement 与 in-flight lifetime tests 全部通过
  - _Requirements: 5.1, 5.9, 5.10, 6.5, 6.6, 6.7_
  - _Boundary: TMResourceResolver Lifecycle and Failure_

- [ ] 4. 实现 current-segment mixed retrieval adapter

- [x] 4.1 映射 canonical current-segment 查询并消费 production retrieval
  - 使用 raw 当前 source 作为 query source、raw speaker 作为 speaker identity；没有正式 context 时传 `None`，不擅自把相邻 UI 段当 Core context
  - 每次阈值变化构造新的查询，传入当前 minimum similarity 与固定 limit 10
  - 原样消费 production `TMRetrievalService` 的 match type、similarity、matched source 与稳定顺序，不重算评分或证明
  - 完成时，exact/context/fuzzy、0.60 inclusive、低于阈值和 1.00 fuzzy 的 adapter tests 使用 Core 结果通过
  - _Requirements: 1.1, 1.4, 1.5, 3.4, 3.5, 3.6, 3.7, 3.10, 9.7_
  - _Boundary: EditorTMAdapter Canonical Query_

- [x] 4.2 实现 query-time legacy exact compatibility port
  - 保持 source-LWW、direct exact 优先和严格 same-speaker Ren'Py alias；无法安全解包时拒绝兼容命中
  - legacy 只产生 exact，不能因 mixed canonical 资源存在而获得 context/fuzzy
  - legacy path/read 失败只返回该资源 safe failure，不吞掉 canonical 成功结果
  - 完成时，direct/alias/unsafe alias/LWW/local failure tests 均保持 raw speaker identity 与 exact-only
  - _Requirements: 2.1, 2.7, 6.5, 6.6, 9.1, 9.2, 9.6_
  - _Boundary: EditorTMAdapter Legacy Query_

- [x] 4.3 聚合 mixed 结果并输出安全 UI projection
  - 合并 legacy 与 canonical exact lane，随后保持 canonical context/fuzzy 的 Core order
  - 以 resource/record 或稳定 legacy identity 去重，在全部资源汇总后只应用一次 global top-10
  - 将 Core report 映射为 frozen suggestion、capability 与 resource status，区分真正 no-match 和 failure/degraded
  - 完成时，跨资源 exact-first、fuzzy score、ties、重复查询、双 source 和全局十条测试结果稳定
  - _Requirements: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 2.2, 2.3, 2.4, 2.5, 2.6, 6.5, 6.6, 9.8_
  - _Boundary: EditorTMAdapter Mixed Aggregation and Projection_

- [x] 4.4 建立确认译文的 TM append port
  - 只遍历 snapshot 中 Active+Update 的 TM ports，分别调用 canonical 与 legacy 的正式 append 能力
  - 返回 per-resource structured write report；无 writable TM 时沿用既有确认语义，不制造虚假写入
  - Update=false 资源不进入写路径，资源失败不被折叠为普通成功
  - 完成时，canonical/legacy/mixed/no-writable/partial-failure 与 Update=false byte-hash tests 全部通过
  - _Requirements: 4.6, 4.7, 9.1_
  - _Boundary: EditorTMAdapter Confirmed Append_

- [ ] 5. 在 EditorController 闭合查询、应用、确认与激活

- [x] 5.1 建立 current query epoch 与 issued suggestion membership
  - project/session、segment/source、resource snapshot、capability snapshot 或 threshold 变化时递增 epoch 并清空旧建议集合
  - 每次当前段查询只使用一次完整 runtime snapshot，原子保存本次最多十条完整 frozen suggestion tuple
  - 状态恢复或资源刷新后自动重新查询当前段；相同状态重复查询保持同集合与顺序
  - 完成时，所有 epoch trigger、in-flight refresh 与重复查询 tests 都能确定旧卡是否 stale
  - _Requirements: 1.1, 2.6, 3.4, 4.3, 6.7_
  - _Boundary: EditorController TM Query Session_

- [x] 5.2 拒绝 stale/tampered suggestion 并只应用目标译文
  - EXACT、CONTEXT 与 FUZZY 一律要求用户显式 apply，不允许自动写 target
  - 校验完整 issued membership，拒绝合法形状下替换 resource、record、target、type、score 或 provenance 的 field substitution
  - 成功只更新当前 target 并保持未确认/dirty 语义，不写 TM、不确认、不跳段；失败保持所有项目与 TM 状态不变
  - 完成时，三种类型成功路径和逐字段 tamper/stale zero-mutation tests 全部通过
  - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
  - _Boundary: EditorController TM Suggestion Apply_

- [x] 5.3 以 structured write report 协调确认与导航
  - 确认当前段时调用 adapter append port，并把每个资源的成功或失败反馈给上层
  - required write 失败时不确认、不导航；Update=false 前后资源 hash 不变
  - 没有 writable TM 时保持既有可确认行为，避免把配置选择变成隐式数据写入
  - 完成时，success/partial failure/no-writable/Update matrix 中 confirmed、dirty、当前位置与资源字节符合设计
  - _Requirements: 4.4, 4.6, 4.7, 9.1_
  - _Boundary: EditorController Confirmed TM Write_

- [x] 5.4 提供首次激活的 preflight、确认与 operation lifecycle
  - 首次激活先返回只读 preflight，显示目标资源、source 与预期状态变化；开始前可取消且零修改
  - 正式开始后创建安全 operation id 与 display phase，禁用重复激活和取消，不向 Qt 暴露 stage/path/token
  - 后台 worker 只调用 Core public contract，其他不冲突的编辑功能继续可用
  - 完成时，preflight/cancel/start/busy/duplicate tests 返回稳定 Controller state
  - _Requirements: 5.2, 5.3, 5.4, 5.5_
  - _Boundary: EditorController Activation Start_

- [x] 5.5 在 activation completion 后重建并原子替换 runtime
  - 成功 outcome 后重新 resolve、re-prove 并一次替换 resource snapshot，再递增 epoch、刷新状态与当前建议
  - proven first failure 保留 legacy；ambiguous facts 显示 unavailable；canonical update 失败保留 LKG 与 source-diverged 状态
  - completion 或 resolver 失败不得发布半套 capability/resource 集合
  - 完成时，success/proven failure/ambiguous/diverged/LKG 的 Controller integration tests 全部通过
  - _Requirements: 5.6, 5.7, 5.8, 5.9, 5.10, 6.7_
  - _Boundary: EditorController Activation Completion_

- [x] 5.6 闭合阈值更新、持久化与重新查询
  - Controller 统一校验并保存阈值，成功后递增 epoch、构造新查询并刷新当前建议
  - 非有限、越界或持久化失败保留旧值、旧 epoch 与当前结果，并返回可理解的 non-blocking outcome
  - 两个 UI 入口只消费同一 Controller 状态，不各自保存第二份 preference
  - 完成时，0.60/1.00/非法/持久化失败/重启/项目切换 tests 全部通过
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 7.7_
  - _Boundary: EditorController TM Threshold_

- [x] 6. 构建本 Integration 拥有的 Qt TM surfaces

- [x] 6.1 (P) 升级当前段 TM suggestion cards
  - 显示 EXACT/CONTEXT/FUZZY、百分比、matched source、target 与 resource；query source 相同时避免无意义重复，fuzzy 时明确实际命中原文
  - 每条卡片提供显式 apply；成功或拒绝沿用 status bar 等非阻塞反馈
  - 将 no match 与 capability/resource failure 分开呈现，不把失败伪装成“暂无建议”
  - 完成时，offscreen cards、三类显示、双 source、apply 与 no-match/failure journeys 全部通过
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 4.1, 4.2, 4.3, 4.4, 4.5, 6.5, 6.6_
  - _Boundary: Qt TM Suggestion Surface_
  - _Depends: 5.1, 5.2_

- [x] 6.2 (P) 在语言资源设置呈现 canonical lifecycle 与操作
  - 每个 TM 资源持续显示 legacy exact-only、canonical active、source-diverged、degraded 或 unavailable 及有限 safe reason
  - 提供显式 activate/rebuild 动作；busy 时禁用重复操作，打开设置、刷新或查询不得触发迁移
  - 单资源失败保持其他资源状态和成功结果，未知内部异常不展示正文、path 或 proof body
  - 完成时，状态、preflight、busy、success/failure/rebuild 与 no-startup-migration Qt tests 全部通过
  - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7_
  - _Boundary: Qt Settings TM Status_
  - _Depends: 5.4, 5.5_

- [x] 6.3 打通两个一致、可发现的 fuzzy 阈值入口
  - Translation Matches 区提供始终可发现、可聚焦且不依赖 hover 的紧凑阈值 chip
  - 语言资源设置的 TM section 提供第二入口；两个入口显示同一有效值、capability state 与 disabled reason
  - 成功或失败用 status bar 等非阻塞反馈；不新增仅靠鼠标悬停才能操作的入口
  - 完成时，Tab/Enter/Space、同步更新、fuzzy unavailable、重启与项目切换 Qt tests 全部通过
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.8, 7.5, 7.6, 7.7_
  - _Boundary: Qt TM Threshold Integration_
  - _Depends: 5.6, 6.1, 6.2_

- [x] 6.4 收紧 Layer 4、可访问性与 offscreen 边界
  - Qt 只依赖 `EditorController` 与 frozen contracts；AST guard 禁止 store、retrieval、evaluator、proof 或 migration implementation 越层导入
  - 所有新增控件提供稳定 object name、accessible name、tooltip、Tab focus 和 Enter/Space 操作
  - persistent capability/resource state 与 transient action feedback 分工清楚，不用 disabled 空壳冒充能力已完成
  - 完成时，AST、accessibility、keyboard 与 offscreen boundary tests 全部通过
  - _Requirements: 1.7, 6.4, 7.5, 7.6, 7.7, 9.4, 9.5_
  - _Boundary: Qt Layer 4 Boundary and Accessibility_

- [ ] 7. 完成 TextMatcher handoff 与 canonical integration 验收

- [x] 7.1 向原 Qt Spec 交付唯一中立 TextMatcher handoff
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

- Task 2.1：新增唯一 application-facing `TMMigrationService.activate_initial(Path, str) -> MigrationOutcome`；合法首次激活仍只经 Core-owned build、`StageSealer`、coordinator prepare/journal/publish 链，非法 source/resource/coordinator、non-READY 与 already-active 在 build 前稳定 fail-closed 且零修改。独立评审 APPROVED；parent fresh completion 覆盖 252 个 migration/activation/sealer 测试（1 个明确 opt-in skip），changed-file basedpyright 0，四个用户 WIP hash 不变。publication tail、rollback/recovery、并发与 tamper 仍分别留给 2.2～2.5。
- Task 2.2：首次激活仅在 generation 0 durable publication、sealed source-binding canonical digest、正式 `SQLiteTMStore` health/revision/query-view 重开全部一致后返回成功；FTS5/fallback 均经默认 fail-closed `TMRetrievalService` 证明 canonical EXACT，同 source variants 保留而 context/fuzzy 继续关闭。独立评审两次拒绝并闭合 ledger 路径一致改绑反例与测试 connection 泄漏后 APPROVED；parent fresh completion 聚焦 4/4（强制 ResourceWarning 无告警）、相关 264/264、changed-file basedpyright 0，四个 WIP hash 不变。published-tail recovery 仍归 2.4。
- Task 2.3：仅在 Core 证明从未发布或 PREPARED/DB_REPLACED/MANIFEST_PUBLISHED 已完整回滚、fresh coordinator 冷复证 legacy authority 后返回 preservation-backed `MigrationFailure`；普通 I/O 完整回滚稳定为 `MIGRATION.INITIAL_IO_FAILED` 且可重试。独立评审三轮拒绝并闭合 source/target basename 置换、builder residue、terminal/quarantine 篡改、primary-error masking；unpublished pair 采用 Darwin `renameatx_np(RENAME_EXCL)` / Linux `renameat2(RENAME_NOREPLACE)` 的 dirfd-relative exclusive quarantine，无普通 rename 回退。最终评审 APPROVED；parent fresh completion mutation-negative 19/19、相关 336/336（1 个明确 opt-in skip）、changed-file basedpyright 0，四个 WIP hash 不变；ambiguous 与 GENERATION_PUBLISHED 仍归 2.4。
- Task 2.4：`GENERATION_PUBLISHED` 返回尾部异常经 fresh coordinator 恢复并复证同一 generation，不重建或生成第二权威；pending/corrupt/无法证明的 durable facts 以封闭互斥的 published-unavailable / ambiguous-unavailable `MigrationFailure` fail-stop，绝不回落 legacy。严格 codec 拒绝 authority 字段剥离、矛盾组合与任意深度 duplicate JSON key；首次激活调用图只归一明确 operational errors，`TypeError`、`AttributeError` 与 `AssertionError` 原样穿透。独立评审两轮拒绝并闭合 authority union 与嵌套 helper 的 programmer-error laundering 后 APPROVED；parent fresh completion 聚焦 81/81、相关 activation/migration/contracts 182/182（1 个明确 opt-in skip）、changed-file basedpyright 0，四个 WIP hash 不变。Gate C/D approved-roots evidence 因本簇源码变化按设计 stale，留待 Task 2.5 后 Cluster A 以累计 reviewed tip 一次刷新，不作为 legacy/canonical authority 结论。
- Task 2.5：首次激活以 Core-private、resource-bound 的持久 reservation 将跨 coordinator/process 的恢复、residue proof、build、seal、publish 与 reconcile 线性化；macOS/Linux 使用 retained no-follow dirfd、短期 parent bootstrap 与长期 `flock`，锁文件只创建一次且不 unlink，进程退出后由 kernel 释放，残留 stage 继续可发现并 fail-stop。锁不可取得时只允许 disposable immutable gen0 证明：不推进/取消 pending journal、不恢复 SQLite sidecar、不注入 live view；已发布权威返回 published-unavailable，未证明事实返回 ambiguous-unavailable。same-inode 内容篡改、foreign inode/symlink/hardlink、并发初始化/进程退出、PREPARED owner、hot journal 与 post-proof drift 均 mutation-negative；既有 explicit import/rebuild 失败仍保留可重开 LKG、SOURCE_DIVERGED 与原 JSONL。七轮对抗性 finding 依次闭合 process latch、restart residue、跨 coordinator TOCTOU、lock bootstrap、pre-lock recovery、completion-only 原子/只读与下游 runtime projection 后，fresh reviewer APPROVED；parent fresh completion 首次激活 72/72、相关 activation/migration/store/LKG 554/554（1 个明确 opt-in skip）、changed-file basedpyright 0，四个 WIP hash 不变。Gate/acceptance/release evidence 继续留给紧随其后的 Cluster A 在累计 reviewed tip 统一刷新。
- 2026-08-18 / Cluster A：native fresh reviewer 对 `8f570250e66687eea0c2a8dac1dcdc6aeb23853e..508aac96b7e81dda0d234d0a64b79d77fac76a76` 累计补丁复审 APPROVED；在 reviewed tip 按 current tracked source 机械重签 Gate A CONTRACTS/matcher build 与 Gate C artifact/build roots，Gate A/C focused 66/66。真实 100k Gate D 以 current-source fingerprint `f463630188cf3a9fb80f670a300863ea1a43829b5b7fc7934058680fefcde96f` 产生 bundle `6d64b45d4833f1eb0bc556b8c452f23579257f02de00d9e2afb308200ca925bf`：FTS5/fallback recall 均为 1.0，migration `66.148/100.876 s`、fuzzy p95 `278.995/285.890 ms`，双路 PASS；fault `61/61`、acceptance `33/33`、release `86/86 GO`，证据/benchmark focused `273/273`。fresh offscreen full suite `1709/1709` / `408.529 s`（1 个明确 opt-in skip），Qt smoke 与字面 5000/200 双路 oracle 的 threshold/top-10 零遗漏；本轮只改 JSON/Markdown evidence，basedpyright 不适用，授权路径 `git diff --check` 通过，四个用户 WIP SHA-256 不变。
- Task 3.1：以 Design 精确四字段 `SuggestionQueryIdentity` 冻结 session/segment/source digest 与聚合 `query_epoch`，后续 resource/capability/threshold 任一变化只递增该总 epoch，不暴露第二份 generation；新增双 source `TMSuggestion`、安全 provenance/resource/retrieval/matcher display projection，并直接复用 Core `TMMatchType`、`TextMatcherState` 与 `TextMatchProfile`。版本化 strict UI codec 拒绝 unknown/missing/duplicate key、bool-as-int、非有限数值、伪造 enum、mutable collection 与 legacy bridge；旧单 source DTO 明确更名为 `LegacyExactTMSuggestion`，只机械维持当前 exact-only Controller/Qt 行为，不进入新 codec 或冒充 issued suggestion。fresh task reviewer APPROVED；parent completion 相关 editor/controller/Qt journeys 70/70、changed-file basedpyright 0、py_compile 与授权路径 diff-check 通过，四个 WIP hash 不变。current-source acceptance/release evidence 随 consumer 身份变化按 B1 cluster 退出点统一刷新。
- Task 3.2：新增冻结 `TMPreferences`，`minimum_similarity` 只接受 exact finite float 的 `0.60～1.00` 闭区间，`result_limit` 只接受 exact int `10`；device-local `workspace.json` 仅持久化阈值，跨项目与重启共享，不写入项目、TM、术语表或网络位置。缺失、损坏或旧 schema 状态只在内存回退 0.60 且不改原字节；非法更新与原子写失败保留旧合同/旧文件并清理 temporary。fresh task reviewer APPROVED；parent completion preference/workspace/editor/Qt 34/34、changed-file basedpyright 0、py_compile 与授权路径 diff-check 通过，四个 WIP hash 不变。Controller epoch、刷新与非阻塞反馈仍归 5.6/6.3。
- Task 3.3：新增 Qt-free exact-only `CapabilityHost`，matcher 初态为 `None` 与 Core `TextMatcherState.UNAVAILABLE` 的单向 display；retrieval 只由 Core default sentinel publisher 构造，并把同一 publisher 注入 production `TMRetrievalService`。初审拒绝公开可变 publisher 导致 capability 已变化而 generation/display 仍旧的 authority 漂移；修正后 caller-facing frozen handoff 只含 generation、安全 display 与 host-owned 只读 `RetrievalQueryPort`，publisher/service/evaluator/manifest/refresh 均留在 host 私有对象图，query 仍只捕获一次 Core snapshot。fresh remediation reviewer APPROVED；parent completion capability/retrieval/validation/editor contracts 184/184、changed-file basedpyright 0、py_compile 与授权路径 diff-check 通过，四个 WIP hash 不变。正式 Matcher/Gate C/Gate D refresh 与原子换代仍归 3.4～3.6，current-source evidence 留待 B1 cluster 统一刷新。
- 2026-08-18 / Cluster B1：native fresh reviewer 对 `97f45840c43946a6b28d16cbff2a34dc5154fdbd..2997794ca5b26562f30c4def3fe7df9f09195a5b` 累计补丁复审 APPROVED；冻结 UI 投影与 strict codec、device-local 阈值、exact-only immutable host 的组合边界闭合，旧 Controller/Qt 只作 `LegacyExactTMSuggestion` 机械隔离，未提前实现 3.4～3.6、resolver、adapter 或新产品行为。累计 reviewer suite 199/199、changed-file basedpyright 0、diff/boundary/placeholder/secret checks 通过。按 current tracked source 机械刷新 fault 61/61 与 acceptance 33/33，release 86/86 GO；Gate D implementation fingerprint 仍与 Cluster A 的已批准 100k bundle 一致，故不重复 benchmark。fresh offscreen full suite 1743/1743 / 408.927s（1 个明确 opt-in skip），Qt smoke 与字面 5000/200 FTS5/fallback 双路 oracle 的 threshold/top-10 零遗漏；evidence focused 38/38，四个用户 WIP SHA-256 不变。
- Task 3.4：在 exact-only `CapabilityHost` 上增加 composition-only Matcher Gate owner；普通 caller 仍只取得 frozen matcher handoff 与只读 generation notification，owner 不接受 manifest、approved-root path、factory、evaluator 或“已通过”布尔，只调用绑定当前 checkout 的 Core `build_validated_matcher_v1`。UNAVAILABLE 强制 matcher=`None`，BASIC 只开放连续文本 profile，TEXT_V1 才开放 Match Case、Whole Word 与 configured terms，纯 CJK 沿用连续语义；refresh 原子递增 generation，旧 handoff/in-flight 保持不变。两轮独立 review 先后拒绝并闭合 byte-identical foreign root 与可替换 module-global factory 两条身份缺口：最终 binding 在调用前后复核 current root、factory callable/module/code/source/defaults/closure/direct globals 与 approved roots，任一漂移只发布新 UNAVAILABLE。全新 explorer 最终 APPROVED；聚焦 19/19、相关 matcher/capability/contracts 117/117、changed-file basedpyright 0、py_compile 与授权路径 diff-check 通过，四个 WIP hash 不变。Retrieval Gate C/D、Controller epoch 与 Qt 控件仍归后续任务。
- Task 3.5：composition-only Gate C owner 从 current checkout 重算配对 `expectation + manifest`，以该 expectation 新建 evaluator、closed publisher 与 production service，再用同一 manifest refresh 并原子换入整份 retrieval handoff；bootstrap sentinel 从不 refresh。成功只递增 retrieval generation 并开放 CONTEXT，fuzzy-core 仅保留 correctness 事实，FTS5/fallback fuzzy 均继续关闭；旧 captured/in-flight query 使用旧 service，下一 query 使用新 graph，matcher generation 独立。任一重算、identity、构造、refresh 或 install 的 operational failure 均保留原 handoff/generation，programmer error 原样穿透。三轮 independent review 依次拒绝并闭合 pre-composition 顶层 callable、递归 helper/canonical alias、Publisher.refresh 与 dataclass 生成方法四类晚绑定缺口；最终通用 source/runtime graph 以 approved build inventory 复核 canonical module/spec/loader、source bytes/inode/digest、imports、MRO/descriptor、显式与生成 callable code/defaults/closure，普通 `CapabilityHost` 仍不加载 offline validator。fresh explorer 最终 APPROVED；focused 20/20、相关 Gate C/host/Core boundary 214/214、changed-file basedpyright 0、py_compile 与授权路径 diff-check 通过，四个 WIP hash 不变。Gate D lifecycle 与 fuzzy execution path 仍严格留给 3.6。
- Task 3.6：composition-private async Gate D owner 只捕获 3.5 已安装的同一 Gate C publisher/service/base manifest；每个 epoch 在平台私有临时区新建并复核 direct `0700` work root，固定 current tracked `benchmark_tm_contract.json`，且 evidence path 调用前必须不存在。只有 Core `run_benchmark_gate_d()` 的 owner-issued result 可经 `publish_retrieval_capability_gate_d()` 刷新该 publisher；正式 publication 后 retrieval generation +1 并复用同一 query port，旧在途 query 保留已捕获 snapshot，下一 query 观察新 capability。runner/identity/cleanup failure 保留 exact/context 与原 generation，现场不递归清理；public host/handoff/status 不泄露 publisher、manifest、runner、result 或 path。fresh reviewer 发现 Core publisher 已 refresh 后 host 仍可能以额外 identity check 把运行翻案为失败，造成 query-effective capability 与 display/generation 分裂；先以精确 RED 固化后，identity gate 收束到 run 与 publish 前，Core terminal fingerprint 成为 refresh 前最后证明，成功 publication 不再事后重分类。最终 reviewer APPROVED；parent Gate D/host/Gate C/matcher/Core suite 134/134、changed-file basedpyright 0、py_compile 与授权路径 diff-check 通过，四个 WIP hash 不变。任务级测试不复用 Cluster A bundle，current-source 真实 100k intended-path run 留给紧随其后的 Cluster B2 出口。
- 2026-08-18 / Task 3.6 B2 remediation：累计 review 暴露 Gate C 换代与 Gate D 后台 publication 竞态，以及 Core publisher、host handoff/display、retrieval generation/notification 和 owner lifecycle 在失败尾部分裂的反例。修正后，Gate C install 与 Gate D 正式 publication 共用窄 lifecycle reservation，100k runner 仍在锁外；Core 以 validate/prepare-before-commit 产生唯一 snapshot 赋值，host 投影、generation/notification 与 owner success 在该 commit 前预构造且异常可回滚。private receipt 绑定 host owner、graph nonce 并一次性消费；formal binding 固定 current-source helper/result/verifier 与 publisher snapshot member descriptor，commit 后不再读取可变 module global、构造 status 或触发可抛通知。多轮精确 RED 覆盖 cross-graph/replay、expired/concurrent Gate C、late helper/cast/descriptor replacement、notification 前后失败与真实 `QueryReport`；最终独立 reviewer APPROVED。parent current-source 相关矩阵 334/334、changed-file basedpyright 0、py_compile/diff-check 通过，Gate C roots 四项与当前文件精确一致，四个 WIP hash 不变。本修正只闭合 ADR-009 与已批准 Integration Design 的原子发布语义，未改变 authority、持久格式、发布协议、依赖方向或跨 Spec frozen contract，因此不新增 ADR/Steering disposition；真实 100k 与 evidence/full-suite 仍留给 Cluster B2 出口。
- 2026-08-18 / Cluster B2 cumulative remediation：累计 reviewer 又发现 3.4 Matcher 与 3.5 Gate C 的 generation notification 仍位于 handoff/authority 切换尾部；`notify_all()` 在 generation 赋值前或后失败时，可造成调用失败或返回旧 handoff，但 host 已暴露新 matcher/CONTEXT authority。修正后两条路径均使用 prevalidated generation 与 provisional commit；Matcher 失败继续传播原异常，Gate C 保留返回旧 handoff 的既有语义，但 publisher/service/base manifest、handoff/status、generation/notification 在通知失败时均恢复为旧 identity。四条精确 RED 覆盖 Matcher/Gate C 各自的赋值前与赋值后失败；累计 reviewer 复审 APPROVED，parent current-source 相关矩阵 338/338、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。治理事件门复核仍为 follow existing ADR-009/Integration Design，无新 ADR/Steering trigger。
- 2026-08-18 / Cluster B2 出口：fresh reviewer 对 `da1e5ebfd32dcb0b1af316e8086ec216037c98d8..1dbd29a` 的 Matcher Gate、Gate C、Gate D 与累计 remediation 复审 APPROVED，未提前实现 resolver、Controller 或 Qt 产品行为。current-source implementation fingerprint `07287467ca3cc262545616fa394016f28f7bc44c41b9dfc591a466c62d76f1ef` 的真实 100k Gate D 生成 bundle `123b2afe1d8f4d1c51dd28c40ddeb8e765abdedad3873e0c72c07b805e0258b0`：FTS5 exact/fuzzy p95 `0.454/290.288 ms`、migration `69.789 s`、RSS `254.594 MiB`；fallback `0.515/294.657 ms`、`104.601 s`、`260.938 MiB`；两路 recall `1.0`、threshold/top-10 零遗漏且 PASS。fault `61/61`、acceptance `33/33`、release `86/86 GO`；fresh offscreen full suite `1822/1822` / `449.745 s`（1 个明确 opt-in skip），Qt smoke 通过，字面 5000/200 FTS5/fallback 双路 `missing_above=0` / `missing_top10=0`（`263 s`）。changed-file basedpyright 0、py_compile、strict JSON 与授权路径 diff-check 通过，Gate C roots 与 current source 精确一致，四个 WIP hash 不变。本出口未产生新 ADR/Steering trigger；可进入 Task 3.7 resource resolver。
- Task 3.7：以完整 declarative global order 和连续 canonical Core order 构造冻结 runtime snapshot；legacy/canonical ports 保留 Active/Lookup/Update，并只机械委托既有 exact/append owner。初审发现 ports 仅有元数据、binding identity 与 snapshot invariants 不闭合；补救后又以对抗窗口发现 canonical/legacy 被二次判定。最终 default open 每个资源只构造一次 `TMEngine`，由同一次分类返回 closed-union runtime binding，且无 caller-forced legacy 旁路。fresh reviewer APPROVED；parent focused/related 212/212、changed-file basedpyright 0，四个 WIP hash 不变。lifecycle/status/atomic replacement、same-speaker alias 与 mixed write dispatch 仍分别归 3.8、4.2、4.4。
- Task 3.8：runtime snapshot 在发布前重验 nested status/port/handle 合同，并把 lifecycle 投影封闭为 legacy exact-only、canonical active、source-diverged 或 unavailable；该层不得以 `DEGRADED` 或 context/fuzzy 可用性形成第二 capability authority。path/open/source-binding/query-lease 故障只生成 resource-local safe status，canonical identity、generation 与 binding/health digest 不闭合时不创建 port、也不回落 JSONL；同一 lineage 内合法 `VERIFIED_CURRENT ↔ VERIFIED_HISTORY` 保持 canonical active，`VERIFIED_* → SOURCE_DIVERGED` 保留 LKG，反向恢复仍 fail-closed。`TMRuntimeHost` 仅在完整候选通过配置谱系复核后一次换代，旧 snapshot 引用继续存活。fresh reviewer APPROVED；parent source-binding/lifecycle/resolver 聚焦 98/98、changed-file basedpyright 0，四个 WIP hash 不变。
- Task 4.1：canonical adapter 每次从同一 host-issued operation 捕获一份防御性 runtime snapshot 与 retrieval handoff，按 Active+Lookup 连续重编号 canonical cohort，并以 raw source/raw speaker、空正式 context、device-local threshold 与固定 limit 10 新建 `TMQuery`；真实 activated SQLite 覆盖 EXACT/CONTEXT/FUZZY、0.60 inclusive、低于阈值与 distinct-source 1.00 FUZZY。为闭合同 publisher Gate D 刷新窗口，Core-private、service-bound、single-use receipt 在 publisher 锁内只捕获一次 immutable capability snapshot，查询期间不持 publisher lock；普通 Core query 仍保持一次 snapshot，正式 Gate C/D publication 由 host lifecycle reservation 等待在途 operation。adapter 逐层重验 Core result/failure/metadata/recall/report 合同、cohort accounting、source/threshold/limit/tie 与 Feature 5 冻结顺序，只验证不重排、不重算 scorer/proof；runtime published/private graph 从同一封闭 candidate 派生，programmer error 不被 drift 归一化。两轮定点评审 findings 闭合后最终 APPROVED；parent current-source host/adapter/runtime 161/161、Core contracts/retrieval/capability/validation 201/201、changed-file basedpyright 0，Gate C roots 随本任务 Core source 机械刷新且正式 validation 44/44，四个 WIP hash 不变。本切片只落实 ADR-009/011 与批准 Design，不改变五类语义门，因此无新 ADR/Steering disposition。
- Task 4.2：legacy query 只消费 4.1 已签发的 `_CanonicalQueryBatch.runtime`，不再次捕获 resource/capability；按 declarative global order 查询 Active+Lookup legacy ports，每资源 direct source-LWW EXACT 优先，direct miss 后才调用既有 strict same-speaker Ren’Py alias/unwrap bridge。unsafe speaker/wrapper 不形成命中，非 EXACT/1.0 或 source 不闭合的 backend 值拒绝提升；批准的读取类 operational error 只生成含 resource/order/stage、固定 safe code 与 retryable 的 body-free local failure，programmer error 原样传播，canonical batch identity 保持不变。private legacy result 暂保留 exact raw record body供 4.3 生成不可逆稳定 identity，不提前实现 mixed merge、UI projection、global top-10 或 append。parent legacy/canonical/resolver/lifecycle/Ren’Py/TMEngine/Controller 相关 111/111、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变；无五类语义门变化或新 ADR candidate。
- Task 4.3：public `query_current()` 只消费一次 4.1 host-issued canonical operation，并让 legacy lane 复用同一 runtime/capability batch；legacy/canonical EXACT 按完整 declarative global order 合并，随后原样追加 Core CONTEXT/FUZZY，聚合后以 resource/record identity 去重并只截一次 global top-10。冻结 `TMSuggestionReport` 统一绑定 raw-source digest、session/segment/query epoch、双 source、Core match type/similarity、安全 provenance、resource status 与 retrieval display；legacy record identity 仅由原始 source/target body 的 domain-separated SHA-256 派生。adapter 重验 Core report/cohort/order 与 capability/resource authority，局部失败只投影 safe stage/code/retryable，partial proof 保留已证明 exact/context 且不从 metadata 提升 capability；不读取 evidence/proof/scorer/path，也不做二次阈值过滤或排序。parent mixed/canonical/legacy focused 33/33、相关 contracts/runtime/host 75/75、Core contracts/retrieval 127/127、changed-file basedpyright 0，py_compile/diff-check 通过，四个 WIP hash 不变；本切片仅落实 ADR-009/011 与批准 Design，无五类语义门变化或新 ADR candidate。
- Task 4.4：`append_confirmed()` 每次只捕获一份 defensive runtime snapshot，按完整 declarative global order 遍历 Active+Update legacy/canonical ports，并以同一 `TMRecordDraft` 调用 3.7 的正式 append seam；Lookup 不影响写回，inactive/Update=false 不进入写路径。既有 `WriteReport` 向后兼容补充 exact `TMResourceWriteOutcome` tuple，新 adapter 报告的 written ids 与 body-free safe-code errors 必须由逐资源 outcome 严格投影；no-writable 保持成功且写入数为 0，partial operational failure 继续后续资源且不泄露正文/path。existing-owner legacy `save_record=False` 只通过 exact typed、空正文的 formal error 归一；同文案普通 `RuntimeError`、SQLite `ProgrammingError/NotSupportedError` 与其他 programmer error 原样穿透。parent focused 9/9、相关 94/94、Qt 68/68、旧 Controller/contracts 36/36、changed-file basedpyright 0，py_compile/diff-check 通过，Update=false 字节与四个 WIP hash 不变；本切片落实批准 Design 的 append/structured-report 边界，无五类语义门变化或新 ADR candidate。
- 2026-08-18 / Cluster C 出口：fresh cumulative reviewer 对 `6bf429614578d5eedb13f068eb0fcdb28621972c..6e4f96730cfbef043eec7ca4217a827b732569cf` 的 resource resolver/lifecycle 与 query/write adapter 累计补丁复审 APPROVED，findings 为 0；reviewer focused/related `272/272`、Gate/validation/module-boundary `102/102`，parent fresh cumulative `338/338`。按 current tracked source 刷新 fault `61/61`、acceptance `33/33` 与 release `86/86 GO`；真实 100k Gate D 以 implementation fingerprint `12dff3c0cba70816e124b183d87228a1e1f6afa9912569b002659ae9a7d17e55` 生成 bundle `4f22bfe70d3abd7d22f96398dfeacf98676c98498e08f6ef0d452b34ea33c88a`，FTS5/fallback recall 均为 `1.0`，migration `67.310/100.332 s`、fuzzy p95 `277.277/283.666 ms`，两路 PASS。fresh offscreen full suite `1904/1904` / `430.991 s`（1 个明确 opt-in skip），Qt smoke 通过，字面 5000/200 FTS5/fallback 双路 `missing_above=0` / `missing_top10=0`（`250 s`）；evidence focused `133/133`、changed-file basedpyright 0、py_compile、strict JSON 与授权路径 diff-check 通过，四个用户 WIP SHA-256 不变。本簇只落实既有 ADR-009/011 与批准 Design，未改变五类语义门，因此不新增 ADR 或 Steering disposition；可进入 Cluster D Controller 5.1～5.6。
- Task 5.1：`EditorController` 新增 opaque project session、聚合 `query_epoch` 与私有 issued membership；项目/段/source 在 Controller mutation 边界立即失效，runtime/retrieval generation 与 device-local threshold 通过 adapter application-private operation stamp 在读取/查询时同步，变化先清空旧集合并以新 epoch 自动重查。每次尝试仍只捕获一个完整 runtime/capability operation；在途 refresh 的旧 report 被丢弃，稳定重复查询保持 epoch、集合与顺序。Controller 对 report、query identity、provenance、status 与 suggestion 建立私有防御副本，外部即使用 `object.__setattr__` 篡改返回值也不能污染 membership；5.2 才消费该集合执行 apply。初始 RED 为缺少 `tm_adapter` query-session seam；parent focused/related `134/134`、Qt `68/68` 与 smoke、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。本任务只落实批准 Design 与 ADR-009/011，不触发五类 ADR/Steering 门。
- Task 5.2：`EditorController.apply_tm_suggestion` 只接受当前 query identity 下逐字段完全匹配的 issued suggestion；resource、record、target、match type、score、双 source、provenance 或 epoch 任一替换均 fail closed。EXACT/CONTEXT/FUZZY 成功路径只更新 target，并保持 unconfirmed/dirty、不确认、不跳段、不写 TM；临时 legacy Qt bridge 也改为先签发防御性 membership，删除旧 source-only bypass。runtime 与 retrieval generation 通过 application-private 窄 reservation 线性化到 target commit，换代不得穿过已验证建议；回调自身的 programmer `ValueError`/`TypeError`/`AssertionError` 原样传播，仅 exact private generation-change signal 归一为 stale。初始 RED 为新 `TMSuggestion` 被旧 legacy-only入口拒绝；并发补充 RED 证明 runtime refresh 曾可在 membership 校验后、target commit 前越过。累计 host/adapter/controller `232/232`、Qt `68/68` 与 smoke、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。本任务继续落实批准 Design 与 ADR-009/011，只增加 Implementation Note 信息，不触发 ADR/Steering 门。
- Task 5.3：配置 `EditorTMAdapter` 时，`EditorController.confirm_current` 只消费 adapter 对一次 captured Active+Update cohort 返回的 exact structured `WriteReport`；每个 attempted resource 的成功/失败均保留，任一 failure 阻止 confirmed 与导航，但不抹除已经发生的资源写入事实。空 writable cohort 返回空成功报告并沿用既有确认行为；Update=false/inactive 资源不被调用且 byte hash 不变。Controller 在项目锁内闭合 target snapshot、append、确认与导航，成功后递增 query epoch；非 structured report、nested contract drift 与 programmer error 均在项目状态改变前显式失败。初始 RED 证明旧 Controller 未调用 adapter、partial failure 仍确认跳段且忽略 adapter 异常。累计 adapter/controller/contracts `134/134`、Qt `68/68` 与 smoke、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。本任务只连接已批准的 4.4 append port 与既有 Controller 确认语义，未触发 ADR/Steering 门。
- Task 5.4：新增 frozen `TMActivationPreflightView` 与 `TMActivationOperationView`；前者只投影资源身份与 valid/invalid/variant counts，后者只投影 opaque operation id、`ACTIVATING/COMPLETED`、完成/成功状态和安全 code。Controller 私下持有 exact Core preflight、`TMMigrationService` 与 coordinator，start 前重验完整 source digest、diagnostics、resource path/flags/name；取消只撤销未开始的 issued preflight且零修改。正式调用在 daemon worker 中单飞执行 `activate_initial()`，开始后拒绝重复 start/cancel，其他编辑仍可用；Core safe failure 只进入 body-free status，programmer error 由 wait seam 原样暴露而 UI status 不含正文/path/token/evidence。初始 RED 为 activation view/API 缺失。累计 Controller/Core activation `120/120`、Qt `68/68` 与 smoke、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。本任务实现批准的 Physical Activation Controller 边界，沿用既有 Core authority 与发布协议，未触发 ADR/Steering 门。
- Task 5.5：activation worker 只在 exact `MigrationReport` / `MigrationFailure` 与重新 resolve 的完整 runtime candidate 闭合后换代；成功唯一发布 canonical active，proven first failure 保留 legacy，ambiguous facts 发布 unavailable，真实 external source 变更后的 rebuild 失败保留 LKG canonical 并精确显示 source-diverged。`TMRuntimeHost` 从同一 closed candidate 派生 private/published snapshots，应用 precommit validator 通过后才一次换代；Controller query lock 跨越 runtime publish、epoch 递增与 baseline 更新，并发查询只能在整体完成后看到新 generation/epoch/canonical 建议。resolver/outcome mismatch 保留旧 runtime identity；若 Core 成功、published 或 ambiguous 事实已禁止 legacy，则刷新失败后锁存 body-free `TM.ACTIVATION.RUNTIME_REFRESH_FAILED` 并拒绝旧查询，不发布半套 resource/capability 状态。初始 RED 中 success/proven/ambiguous 三路 runtime generation 均停在 0；完成后累计 activation/runtime/adapter/controller `152/152`、Qt `68/68` 与 smoke、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。本任务只闭合批准 Design 的 runtime replacement 与 ADR-009/011 既有边界，未触发五类 ADR/Steering 语义门。
- Task 5.6：Controller 新增唯一 `update_tm_minimum_similarity()` 入口与 defensive `tm_preferences()` view；前者在 query lock 内按“exact finite 0.60～1.00 验证 → device-local atomic persistence → epoch 递增/baseline 换代 → 当前段 production query”的唯一顺序执行，成功后 Controller 持有新 issued report，两个后续 Qt 入口不需保存第二份 preference/report。新 frozen `TMThresholdUpdateOutcome` 只含 success、当前 preference 与 safe code，不携带 suggestion 正文或 persistence error body；非有限、越界、bool/int/float subclass 和真实 `os.replace` 失败分别返回 `TM.THRESHOLD.INVALID` / `TM.THRESHOLD.PERSISTENCE_FAILED`，旧 preference、workspace bytes、epoch 与 issued suggestions 均不变。边界 0.60/1.00、重启、项目切换与新查询均有真实 Controller/workspace/adapter 验收。初始 RED 为 outcome 合同导入缺失；完成后相关 Controller/preferences/workspace/adapter `101/101`、Qt `68/68` 与 smoke、changed-file basedpyright 0、py_compile/diff-check 通过，四个 WIP hash 不变。本任务只闭合批准 Design 的 device-local threshold 流程，未改变 authority、持久格式、发布协议、依赖方向或跨 Spec frozen contract，因此无 ADR/Steering candidate。
- 2026-08-18 / Cluster D 原生 implementer 补救审计：按 v2 簇工作流对 parent 先行实施的 Tasks 5.1～5.6 重做目标业务验收，并闭合了三类信息增量：raw speaker 纳入 query epoch 与 legacy issued membership；Controller 资源增删改与 activation/rebuild completion 以同一 query-lock 边界换代 runtime、compatibility engines、epoch/baseline 并自动重查当前段；activation/rebuild candidate 额外与 Core outcome 的 generation/canonical store identity 闭合，public rebuild 失败保留 LKG/source-diverged，成功立即更新当前建议。Task 5.2～5.4 的 membership/apply、structured confirm 与 preflight/singleflight 未发现额外缺陷；Task 5.6 补充了全新 Repository/RuntimeHost/Adapter/Controller 重启后 production query 实际消费 0.83 阈值的验收。原生 implementer focused `51/51`、related `326/326`、Qt `68/68` 与 smoke；parent fresh completion focused `51/51`、跨层 `299/299`、Qt `68/68` 与 smoke，changed-file basedpyright 0、py_compile/scoped diff-check 通过，四个 WIP SHA-256 不变。这些只是对批准 Design 与 ADR-009/011 的实施闭合，未跨越五类语义门，不新增 ADR candidate。
- 2026-08-18 / Cluster D 累计评审 remediation：fresh native cluster reviewer 首轮拒绝 `62e5790..b717361`，并以真实 Controller 业务路径复现三个跨任务缺口：repository 已持久变更而 runtime refresh 失败时旧 Active+Update port 仍可写入乃至复活已删除 TM；capability generation 变化只惰性失效而未自动重查；published/LKG completion 未把同 generation candidate 绑定到本次 Core service 的 exact canonical store identity。原生 implementer 聚合补救后，所有 repository-before-runtime 变更在 refresh 失败时立即递增 epoch、清空 membership 并锁存 body-free `TM.RUNTIME.REFRESH_FAILED`，成功完整换代和 production query 后才解除；observer 以非递归查询原子完成 invalidate/requery 并在在途二次换代时只发布最终 epoch；activation worker 将实际调用过的 service canonical store identity 贯穿 success/published/LKG 与 compatibility-engine precommit。精确 RED 为 `7/7` 失败（`8` 个断言），GREEN 后 focused `60/60`、related `326/326`、Qt `68/68` 与 smoke、boundary `6/6`、changed-file basedpyright 0 及 py_compile/scoped diff-check 通过；额外 full discover `1957` 项仅 `2F+1E` 为本次 current-source 改动后尚未刷新的 acceptance/release evidence digest，无行为测试失败，四个 WIP SHA-256 不变。这是既定 Controller 原子性与 Core identity proof 的恢复，不改变五类语义门，不新增 ADR candidate。
- 2026-08-18 / Cluster D 定点复验 remediation：同一 cluster reviewer 在新 tip 又复现“最终 generation 校验已返回、report/membership commit 未执行”窄窗口内的第二次 Gate C 换代，旧 generation report 会被发布。修复不新造 authority：复用 adapter 既有 runtime+retrieval generation reservation，只在其中线性化 `_observed_tm_signature` / `_current_tm_report` / `_issued_tm_suggestions` 三项私有引用赋值；exact generation-change signal 仅递增一次 epoch 并重查，programmer error 原样传播。精确 RED `1/1` 与 GREEN 后 focused `61/61`、CapabilityHost/Gate C/D/adapter related `128/128`、Qt `68/68` 与 smoke、boundary `6/6`、changed-file basedpyright 0、py_compile/scoped diff-check 均通过，四个 WIP SHA-256 不变。该修复仅闭合既批准的 Controller commit linearization，不触发 ADR candidate。
- 2026-08-18 / Cluster D 出口：fresh native cumulative reviewer 对 `62e579093a3667ef97c704eabb10d4df74bedd01..51059c5098792dae9737f34f651df11122f01c67` 的 Tasks 5.1～5.6 累计补丁最终 APPROVED；定点重放确认资源持久变更失败后旧写入权威不可用、deleted TM 不复活、capability 换代自动重查只发布最终 generation，success/published/LKG 拒绝 foreign service store，且 apply/confirm/preflight/threshold 无回归。parent 按 current tracked source 刷新 acceptance `33/33`、fault `61/61` 与 release `86/86 GO`，source fingerprints 分别为 `edb9ad9990171969af2c328f45aac5ba4ec410214b682cde3b09de2a9b35ea84` / `701ccae34a7962c5f45ca067fff104c8436a9fb997ec6c5264f29f10ef0e3fae` / `4e2d55f222c36f9dec0adc47f6fa13658002af1bf0f4ae5ea29b6f84e152f835`；Gate D implementation fingerprint 未变，继续使用 Cluster C 已验证的 bundle，未机械重跑 100k。fresh offscreen full suite `1958/1958` / `438.497 s`（1 个明确 skip）、Qt smoke 通过，literal 5000/200 FTS5/fallback 双路 `missing_above=0` / `missing_top10=0`，evidence integrity `38/38`、strict JSON 与授权路径 diff-check 通过，四个用户 WIP SHA-256 不变。本簇只恢复既批准 Controller 状态机、ADR-009/011 与 Core identity 边界，未跨越五类语义门，不新增 ADR 或 Steering disposition；可进入 Cluster E。
- Task 6.1：Qt 只通过真实 `EditorController.tm_suggestion_report()` 渲染当前段 TM 卡片，展示 EXACT/CONTEXT/FUZZY、百分比、target 与 resource；query source 与 matched source 相同时不重复，FUZZY 时明示实际命中原文。每张卡片的显式 apply 继续受 Controller issued membership 约束，成功或 stale 拒绝只用 status bar 非阻塞反馈；无结果、capability closed 与 resource query failure 分开呈现。Controller 新增的只读 report-seam 装配事实不是 capability，term-only business API 使新 UI 无需额外运行旧 TM query；未注入 adapter 的 legacy Qt journeys 保持兼容。初始 RED `3/3` 精确停在 current report/card/state 缺失；GREEN 后 authentic card journeys `3/3`、完整 Qt offscreen `71/71` 与 smoke、Controller/adapter related `91/91`、changed-file basedpyright 0、py_compile/scoped diff-check 通过，Qt AST 边界未导入 Core store/retrieval/proof/scorer/capability owner，四个 WIP SHA-256 不变。本任务只消费既批准 frozen report/Controller 边界，不新增 ADR candidate。
- Task 6.2：`EditorController.tm_resource_statuses()` 是 Qt 唯一 lifecycle 业务入口；它通过 adapter/host 复用同一 resolver 重新观察 resource facts，不 query、不 migration、不替换 runtime 或递增 generation，因此 canonical 激活后外部 JSONL 变化可在普通设置刷新时如实显示 SOURCE_DIVERGED，无需伪造配置 mutation。设置页每个 TM 行持续显示 legacy/canonical/diverged/degraded/unavailable、Exact/Context/Fuzzy 投影与有限 safe reason；只有用户显式确认才调用 Controller preflight+activate 或 rebuild，运行期间全局禁重，Qt 只保存 operation id 并以 75 ms body-free poll 更新，取消、失败和未知异常均不泄露 path/proof/body。初始 RED `7/7` 精确停在 status/action 缺失，另以真实外部变更补充 SOURCE_DIVERGED RED；GREEN 后 focused `7/7`、完整 Qt offscreen `78/78` 与 smoke、Controller/adapter/runtime/CapabilityHost related `134/134`、changed-file basedpyright 0、py_compile/scoped diff-check 通过，Qt 仍只导入 contracts+Controller，四个 WIP SHA-256 不变。本任务只补齐既批准 Controller-only lifecycle display/action use case，不新增 ADR candidate。
- Task 6.3：Translation Matches 与语言资源设置各提供一个共享 device-local fuzzy threshold chip；两者每次只从 Controller 的 defensive preference/retrieval display 读取同一值与 capability 状态，更新仍只调用 `update_tm_minimum_similarity()`，成功、非法值与 persistence failure 通过 status bar/inline status 提供 body-free 非阻塞反馈。fuzzy unavailable 时入口持续可见并可由 Tab 聚焦，`fuzzyAvailable=false` 同时驱动禁用样式、可访问原因与功能门，鼠标、programmatic click、Enter、Space 均不得打开编辑或提交更新；可用时两个入口支持 Tab/Enter/Space、同步刷新，并跨项目与全新 Controller 重启恢复同一值。初始 RED `4/4` 停在双入口控件缺失；parent completion 又以 unavailable `setEnabled(False)` 无法 Tab 聚焦形成定点 RED并闭合。最终专测 `5/5`、完整 Qt offscreen `83/83` 与 smoke、Controller/CapabilityHost/adapter related `43/43`、changed-file basedpyright 0、py_compile/scoped diff-check 通过，Qt 未取得 publisher/evidence/retrieval/store authority，四个 WIP SHA-256 不变。本任务只消费 5.6 的唯一 preference owner 与 frozen capability display，不新增 ADR candidate。
- Task 6.4：累计审计 6.1～6.3 的真实 Qt surface 后，强化 AST guard 以禁止 store/retrieval/evaluator/proof/migration 等 implementation family 越过 Layer 4；TM apply、lifecycle action、threshold/resource/suggestion persistent state 均补齐稳定 object name、accessible name、tooltip 与键盘合同。apply 使用真实 Tab 到达并支持 Enter/Space；资源表格内 lifecycle action 不再只声明 `StrongFocus`，设置页每次 refresh 后显式建立 threshold→enabled lifecycle action→table 的焦点链，因此嵌套 cell navigation 不会吞掉动作。persistent capability/resource state 始终由 label/inline state 呈现，取消、成功与失败仍只进入 transient status feedback。初始 accessibility RED 为 `3` 项，追加 apply Return 与连续 `16` 次 Tab 不可达的精确 RED 后闭合；parent completion 完整 Qt offscreen `87/87` 与 smoke、focused Layer 4 `4/4`、changed-file basedpyright 0、py_compile/scoped diff-check 通过，Qt import boundary 未取得 Core authority，四个 WIP SHA-256 不变。本任务只收紧批准的 Layer 4/accessibility 边界，不新增 ADR candidate。
- 2026-08-18 / Cluster E 累计评审 remediation：fresh native cluster reviewer 对 `4f56645881214e181b839b65ab1f329e806f513a..7416ac82f25b820f14285aebc9fc2f273990dd27` 首轮 REJECTED，并以真实 Qt/Controller 路径复现五类跨任务缺口：production bootstrap 未装配 CapabilityHost/TMRuntimeHost/EditorTMAdapter；resource lifecycle 与 retrieval capability 显示跨 generation 分裂；任意异常正文可伪装 safe code；全局 runtime refresh block 被错误归到历史 activation 资源；阈值已持久化但 current report 刷新失败时 UI 仍声称值未改变。补救后，`qt_editor.py` 每次应用运行只建立一套正式 composition，exact-only UI 立即可用，daemon 按 Matcher TEXT_V1→Gate C→Gate D 顺序验证且 programmer error 进入 `threading.excepthook`；Controller 的 retrieval/resource status 以 no-query fresh generation 投影失效旧 membership，runtime block 统一关闭整个 TM cohort；阈值 outcome 如实区分“已保存、建议未刷新”，Qt 只展示 exact typed allowlist safe code。精确 remediation RED `6/6`，追加 no-query/programmer-error 与 generation race RED 后累计 `11/11` GREEN；parent fresh completion 核心矩阵 `81/81`、完整 Qt `87/87` 与 smoke、bootstrap `8/8`、resource/runtime `52/52`、changed-file basedpyright 0、py_compile/scoped diff-check 通过，捕获输出无后台线程 traceback，四个 WIP SHA-256 不变。该补救只恢复批准的 composition、ADR-009 单一 capability authority 与 body-safe UI 边界，未跨越五类语义门，不新增 ADR candidate 或 Steering disposition。
- 2026-08-19 / Cluster E 定点复验 remediation：同一 cumulative reviewer 在 `1cc3e30f9af3596357ad76590300a1ac698b11fd` 复验时确认首轮五类 finding 已闭合，但以真实 window + formal Gate C/fake Gate D 路径复现后台 capability 已恢复而当前 report、threshold chip 与 epoch 仍停在 exact-only，必须等待用户操作才刷新。补救把 validation 启动移到 QApplication/window 建立后，使用 Qt queued signal 将 Gate C generation 变化与 Gate D 异步完成分别交回主线程调用既有 `refresh_suggestions()`；worker 只 emit，Gate D wait 只阻塞 daemon，receiver 已销毁时 Qt 自动断连，programmer error 仍进入线程异常钩子。两条精确 RED 分别覆盖 Gate C/Gate D 两次自动刷新和 window DeferredDelete 后 late completion；GREEN 后 remediation `13/13`、完整 Qt `87/87` 与 smoke、bootstrap `8/8`、Controller no-query `32/32`、真实 subprocess smoke、changed-file basedpyright 0、py_compile/scoped diff-check 全部通过，捕获输出无 `Exception in thread` 或 traceback，四个 WIP SHA-256 不变。该通知桥只消费正式 capability generation，不改变 authority、持久格式、Core publication protocol、依赖方向或跨 Spec frozen contract，因此不新增 ADR candidate 或 Steering disposition。
- 2026-08-19 / Cluster E 出口：同一 native cumulative reviewer 对固定范围 `4f56645881214e181b839b65ab1f329e806f513a..3595ac9006971d20c419eef45181f7ebee5bb70b` 最终 APPROVED，merge-base 精确等于 base，Critical/Important findings 均为 0；真实 composition、同 generation capability/resource 投影、body-safe exception、全 cohort runtime block、threshold partial-success 与 Gate C/Gate D queued UI recovery 全部重放闭合。parent 按 reviewed tip 重签 fault `61/61`（source `701ccae34a7962c5f45ca067fff104c8436a9fb997ec6c5264f29f10ef0e3fae`）、acceptance `33/33`（source `3eb8027b36fcb1bf3c5423e6e97525d6f2a9e4213409cb088bccaf2521a05484`）和 release `86/86 GO`（source `58ddb1ae66bd655d133bf8eb6459397dbc991d2d27b8947da0139d1c7d5d0b2f`）；Gate D implementation 未变，release owner继续接受 Cluster C 已验证的 bundle `4f22bfe70d3abd7d22f96398dfeacf98676c98498e08f6ef0d452b34ea33c88a`，未机械重跑 100k。fresh offscreen full suite `1990/1990` / `452.069 s`（1 个明确 skip），Qt smoke 通过，literal 5000/200 FTS5/fallback 双路 `missing_above=0` / `missing_top10=0`，evidence integrity `38/38`、strict JSON、scoped diff-check 与四个 WIP SHA-256 全部通过。本簇只落实批准 R/D/T、ADR-009/011 与 Layer 4 边界，未跨越五类语义门，不新增 ADR 或 Steering disposition；可进入 Cluster F Task 7.1～7.3。
- Task 7.1：`EditorController.text_matcher_handoff()` 只读返回本次 composition 的同一 frozen matcher snapshot；BASIC/TEXT_V1 均只经 Core gated port 执行，未装配或验证失效时 fail closed，原 Qt Q1/Q2 不获得本地 matcher fallback，legacy Trie 语义保持原 owner。

- 用户 WIP 基线：`Demo.xlsx de4d85b4dc8ce2e828dea4b2941ad0748f937b307df61d8a3d98f454bbb2bb7f`；`spec.md d781dc2d324b69199d3078ee485a2ca224a9f18c5946f7712c8874af3719b611`；`terms.csv 36ec5fca0895fd0e4f1229a2650b9b5dfe2e3aa87599caeda67c04c68860a837`；`tm.jsonl 82b1597aba42dcc40bcd9404485ed9a1140103713af7872ea5eae1619c1e4f73`。
- 合并前身份基线：授权 UI 根为 `ui-mvp@af23b2a534f3ff061d033470e3112ede309720cc`；授权 Feature 5 source 根为 `feature5@dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b`；两 tip 的 merge-base 与各自相对的历史共同基线均为 `459b524e72ce3d1f3925088669988a0e730cdb39`；UI object database 尚未引入 `dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b`；UI 本地旧迁移 ref 为 `feature5-migrate@fe7afa57bfdf7ac3fc347695c304588f8ad706f2`，不得用于精确 merge 或补齐到 `b90de57…`。
- canonical 红线：legacy importer 的 source-LWW 与当前 100% 卡片只验 exact compatibility；多译文、非 100%、阈值、排序和 fuzzy 必须使用真实 activated SQLite 与 production retrieval API。
- 每个实现子任务经 task-focused validation 与 parent fresh completion evidence 后，使用显式路径暂存该任务文件和本 `tasks.md` checkbox；不得 `git add -A`、`git add .`、stash 或吸收用户 WIP。独立高风险缺陷才按需增加定点 reviewer，不形成固定次数门。
- cluster review 是全部成员小步提交后的累计退出证据：按 `.kiro/steering/feature5-ui-integration-review-clustering.md` 记录 full base/tip、累计 diff 与共享故障矩阵；Checkpoint M/Q 仍只更新 owning Spec，不得在本文件代勾。
- 2026-08-17 / Task 1.3：以 merge commit `482dd5b` 保留精确 `dd7c9fdb268b4ee8ac3545f43e3f5f19e715ff3b` 为第二父；唯一 roadmap 冲突保留 Integration contract 与 Core gate 事实，acceptance/release evidence 由官方工具绑定 merged UI source（33/33、86/86 GO），canonical suite 1629 tests 全绿并保留 1 个明确 opt-in 100k envelope skip；四个用户 WIP SHA-256 未变化。
- 2026-08-17 / G0 cluster：native reviewer 对 `105449a5838763e0a08a05a600a45a457d589edf..9479c4f33d2c540fb300b0773d335dec3ffc7f24` 累计补丁复审通过；fresh cluster suite 在 reviewed tip 运行 1629 tests / 413.467s，Qt smoke、FTS5/fallback 5k/200 oracle 与 release 86/86 GO 全绿，唯一 skip 仍是明确 opt-in 100k migration envelope；WIP hashes 未变化。
