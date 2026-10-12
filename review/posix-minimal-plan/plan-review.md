# POSIX 最小修复审批包独立审阅

日期：2026-10-12。审阅对象：本目录七份候选文档；生产源码基线 `2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5`；外援输入 `b3d5e26e76860ef6f673f1d6f63abb401d55210d:REVIEW-POSIX-SQLITE-BINDING.md`。

## 修稿复审最终结论

**PASS：仅表示候选包已具备提交用户审阅的条件。** 一轮有界修稿后，首轮 F1–F3 已在候选文字与任务依赖中闭合；本次没有发现这些修订引入新的明显合同矛盾。本结论不批准 ADR 采纳、正式 R/D/T 写入、225 个提交重放或生产实施，不代表实现可行性已经实测，更不签发产品或跨平台交付 PASS。原首轮 RETURN_TO_DESIGN 与具体 findings 保留在后文，不改签原结论。

本轮只复审 F1–F3 及相应新接缝：

| 首轮项 | 修稿后结果 |
| --- | --- |
| F1：DB pending 与 receipt SQL | PLAN §3.6 已选定真实 v2 `DB_REPLACED` 命名子阶段：durable journal 复证后，在 pending 原 readback 仍 live 时取得并比对 guard，pending terminal/close 全部成功后才进入 receipt SQL；SQL/statement/backup 结束后再捕获 final。PendingPublication 不增加 detach/rearm，也不修改 preliminary 内容；coordinator 在命名 terminal 后仍 ACTIVATING。guard 获取、terminal、close、SQL或恢复不明均保留真实阶段。冷恢复先验证本次 root/W1 与完整 closure，不靠历史数值接管。 |
| F1：平台／治理界限 | ADR-033 决策10及取代关系明确限定 ADR-020 §9 的 POSIX TM 子阶段，Windows与其他 publisher 不变。平台11.4补已有 `ExistingFileMutationGuard` 的POSIX实现；guard只 fstat retained FD 与 rooted no-follow 名称观察，不在活 SQL 时另开关同 inode 的 DB FD。该端口当前拒绝能力仍明确标为待实现，未伪称已有能力。 |
| F2：SOURCE_DIVERGED | PLAN范围、ADR决策2、治理正文及可直接写入Core R的范围引文均明确保留配置JSONL/manifest外部内容变化的既有合同；canonical及其位置仍可证明时保留Lookup/Update。Core10.7与验证矩阵补入该区别，不放宽原逐文件异常、恶意输入或不明恢复。 |
| F3：有限门任务依赖 | 生命周期门的真实 query/append/lazy view/health/reservation 切片在Core10.1–10.2；v2门的 builder/seal、DB与receipt交接、正常activation必经I/O及指定DB_REPLACED恢复窗口在10.3–10.6。10.7–10.11明确是门后剩余消费者/恢复/导出扩展，不再让门依赖其后置任务。Core10.3–10.6被分成现有owner端口的可检查切片，不另建状态机。 |
| F3：闭合与回归 | 非并行治理闭合项已有owner、授权依赖与可观察结果；它消费人的决定而不代替决定。Windows11.5有实际跨owner依赖、完成条件和现行Requirement映射，保持按真实变化重验及历史证据原范围。contract-discovery不再另追加早期9.9任务号。 |

两个有限门仍是**待审批后执行的技术验证**：同家族短时串行与现有等待/取消预算是否兼容，以及新的窄交接能否贯通正常create/replace与两个DB_REPLACED恢复窗口，尚无本轮运行证据。失败时停止扩展、报告正常场景和具体接口缺口，不默认转向VFS或扩大首发硬门，这一退出边界清楚。

复审机械检查：修订六文件的本地链接目标仍存在；Windows11.5新增引用的10.1、12.1–12.5、12.7均为现行条文。生产HEAD仍`2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5`；`.kiro`、AGENTS、`.opencode`与跟踪Python文件对HEAD无diff，暂存区为空；仍仅原有未跟踪测试/fixture。没有运行产品测试或修改正式规格。

---

## 首轮审阅记录（修稿前）

**首轮结论：RETURN_TO_DESIGN。** 当时的候选包尚不能作为完整接线与任务图的可批准版本。主要阻断是 POSIX DB publication 与命名后 receipt SQL 的阶段交接仍与现有 `PendingPublication` 合同冲突；另有一个支持范围文字冲突及有限技术门的任务依赖缺口。以下保留首轮发现及其当时定位，修稿后状态以上表为准。它们均针对候选设计，不是产品实现验收，不要求 VFS、驱动迁移或扩大首发保护范围。

## 1. 需要修正的 findings

### F1 / P1：receipt SQL 前关闭 retained destination 后，现有 PendingPublication 无法继续完成

**位置**：[PLAN.md](PLAN.md) §3.4（第79行）、§3.5（第89行）；[wiring-discovery.md](wiring-discovery.md) §5；[adr-033.draft.md](adr-033.draft.md) 取代关系。

候选要求 reservation 转成同对象 Candidate，调用原 `begin_publish`，随后在 receipt SQL 前关闭 publisher retained DB raw handle，SQL 结束后重新捕获 final facts。这不是当前 publisher 已支持的接线：

- `platform_fs_contracts.py:1754–1809` 的 `PendingPublication` 持有原 retained destination；`preliminary_facts` 与 `terminal_reproof` 都要求该 authority 仍打开，没有 detach/rearm 端口。关闭后再打开同路径文件不能续接原 pending。
- `platform_fs_posix.py:1668–1687` 的 terminal 要求新的 `PublishFacts` 与 preliminary 完全相等，包含内容事实。`tm_activation_recovery.py:3955` 的 `_publish_activation_receipt` 确实在命名后更新 receipt/binding/activation meta，合法地改变数据库字节；因此即使不提前关闭句柄，跨这次写入也不能保持原 preliminary 内容事实。
- 平台 Design 第315行及 ADR-020 决策9要求 retained readback 跨 owning durable commit 与 terminal。ADR-033 当前只限定决策2/12，没有说明 DB 命名子阶段、receipt SQL 与该约束的关系。

这影响正常 fresh create/replace，不是活事务外部替换的残余窗口。仅把 reservation 变成 Candidate 或加 root 复证无法解决。

**最小修法**：先明确 DB 命名子阶段究竟在哪一个既有 durable phase 闭合。可评估在 receipt SQL 前完成该命名阶段的 journal/readback/terminal 并关闭 PendingPublication，然后在持续原 root/W1 下进入后续 SQL 阶段；但必须证明这与既有 v2 中断恢复、同对象约束和 ADR-020 决策9兼容，不能把提前 terminal 等同于整个 activation 成功。若确实需要改变 pending 的生命期或不变量，候选须列出精确的 POSIX-only 端口、状态/失败事实与 ADR-020 §9／平台 Design 的窄修订；Windows和其他消费者不变。二者选定其一再评审，不能留给实施时自行猜测，也不应因此自动升级到 VFS。

### F2 / P1：外部“直接改写家族”会误把既有 SOURCE_DIVERGED 场景排除

**位置**：[adr-033.draft.md](adr-033.draft.md) 背景第10行、决策2第17行；[governance-delta.md](governance-delta.md) `delivery-boundaries.md` 候选正文。

背景把配置 JSONL 和 manifest 都算作资源家族，决策2却把外部“直接改写活动 TM 家族”整体列为不支持。现行 Core R2.13、R7.8–7.11，以及 Design「Canonical authority 与 JSONL snapshot binding」明确处理配置 JSONL/manifest 外部内容改变、删除或与 ledger 不一致：canonical 仍有效时进入 `SOURCE_DIVERGED` 并继续 Lookup/Update。本包也声明保留该合同。当前总括文字因此会产生两种相反解释，并比用户明确排除的在线目录移动/替换更宽。

**最小修法**：明确排除的是在线移动/替换资源目录、直接干预 active SQLite/内部事务与恢复文件等非协作操作；另外明确配置源/manifest 的外部内容变化仍依既有 `SOURCE_DIVERGED` 合同分类，只要 canonical 与其家族位置仍可证明。逐文件身份异常仍按原严格规则处理，不因这项澄清允许链接、篡改或不明 recovery。将例外同时写入 ADR、范围候选和实际正常验证清单，避免以一处说明去隐式覆盖另一处绝对句。

### F3 / P2：两个有限技术门依赖的真实接线目前排在门后

**位置**：[tasks-and-validation.md](tasks-and-validation.md) 第23–31行及第50–51行。

家族生命周期门要求真实 query/append、嵌套 health、懒 view 和取消；这些实际接入被列为依赖“两门通过”的10.3。v2同对象门要求真实 builder→seal→receipt→cold reopen、已有 generation replace 和一个 pending recovery；receipt/cold recovery 及 journal/manifest/lineage/backup 接线又分别列在10.4/10.5，依赖10.3／10.2。没有写出哪些真实入口必须作为门的最小切片前移，执行者只能提前实现门后任务，或退而用 mock/旧 pathname 路线宣称门通过。

**最小修法**：在任务图显式区分“门用的真实窄切片”和“门后扩展”，无需先实现全矩阵。例如：

1. 生命周期前置切片只接一个真实 store 的短 query/append、lazy view、health 及 replacement reservation，配两 coordinator/两进程验证；10.3改为其余 runtime 消费者与 failure 路径扩展。
2. v2前置切片由 Core 明确的集成任务包含正常 create/replace 必经的 DB/manifest/journal/receipt/lineage 与一个指定恢复 phase，并包含 F1 选定的发布阶段接线；10.4/10.5只扩展其余 schema、reopen、恢复/失败切点。不要把首个切片的实际所需 I/O 留在后置任务。
3. 明确门在这些切片完成后执行、通过仅允许扩展消费者而非宣布功能完成；失败保留实测正常场景与接口缺口并停止依赖项。

10.2现同时覆盖多个集成结果，10.4/10.5也各包含可独立验证的责任段。应在计划定稿前按上述阶段拆到现有 owner 的可执行小任务，而不是写“实施时若过大再拆”。Core协调与平台原语仍各自拥有代码；跨 owner 的全链路检查作为明确 integration task。

此外，治理闭合目前仅是一段前置说明。正式任务候选需要依 `tasks-generation`/governance gate 加一个非并行的实际闭合项，写明 owner、依赖与“新开发基线继承唯一治理提交、owner delta一致”的可观察结果；它应消费人已完成的审批，不把未批准 ADR 变成开发任务。平台11.4也应补成有明确依赖和 Requirements 的完整任务行。

## 2. 已成立的部分

- 主方向准确承接用户与外援：稳定本机目录和协作生命周期是硬要求；已可观察漂移拒绝；活事务外部替换保留为边界观察，不要求全时序零副作用；不引入 VFS、驱动或新平台层。
- R候选整体为支持范围、用户可见成功/失败和局部资源停止，没有写入本轮测试计数、用户游戏名称或审批结果。Resource R12原来就是验证要求，候选是在区分其范围，不能被当作已经通过的证据。
- 通用 backend 与恢复 profile 分开、原 root/W1 的持续借用、admission/drain/W1顺序、禁止将同路径副本重绑为旧授权、懒 view/backup 生命周期、raw FD close 风险均有明确设计输入。
- POSIX旧v2数值观察使用已绑定对象的可选内部端口，不让Core反解opaque identity；fresh reservation 与冷恢复existing stage的准入明确区分。冷恢复适配不足时停止有限门的安排合理，不能据候选文字先宣称该接口已存在。
- 不把 root 失效混为 device-only repair 或 Fuzzy资格；peer资源和项目编辑/保存独立；Sync只回到本机包事务；Windows按实际共享变化回归，历史证据不改签。
- ADR-007决策2/4与ADR-008决策6的限定目标准确。F1补齐前，尚不能确认ADR-020的取代关系覆盖了全部实际接线变化。

## 3. 机械核对

以下只验证计划输入，不执行产品或真实 SQLite 实验：

| 核对项 | 结果 |
| --- | --- |
| Core新增R7.15–7.18 | 现行R7最后是7.14，无重号；四项均在候选任务中有映射 |
| 平台新增R5.6–5.7 | 现行R5最后是5.5，无重号；均映射到平台11.x |
| UI新增R6.14 | 现行R6最后是6.13，无重号；映射到UI10.1/10.2 |
| 修改条款 | Core2.4/2.11/2.12/7.5/7.13/7.14、Resource7.7/8.6/8.7/12.3/12.4、UI5.6/5.9均存在且有任务覆盖；未要求未变全部历史R重新实现 |
| 新任务大号 | Core10、平台11、Resource6、UI10不与现有最高任务冲突；contract-discovery中的早期9.9/5.7建议须在定稿时标为被主图取代，不能同时追加两组 |
| Markdown相对链接 | 七文件中的本地Markdown链接目标均存在 |
| Git区间 | `a057d260…` 到 `2e1cbce…` 为225个提交、0 merge，首个确为`34c7f755…` Windows兼容改造基线 |
| Git治理规则 | 当前HEAD与本地治理tip的repository-safety blob均为`b2df408032b26418830d4cf1da6d9fc7a1fdc8c9` |
| 开发/治理保护 | 当前HEAD仍`2e1cbce…`；治理仍`a057d260…`；四个工作树及分支与方案相符；发行tag`c426e34e…`不是当前开发祖先，方案保留其原对象 |
| 外援输入 | 本地`origin/temp-posix-sqlite-binding-review`解析到`b3d5e26…`，原本地评审分支仍`3dbf2ed…`；只把前者作为输入，不回退开发 |
| 未改正式/生产 | 对HEAD的`.kiro`、AGENTS、`.opencode`及跟踪Python文件diff为空，暂存区为空；仅原有未跟踪manual_package fixture与两份测试文件 |

本审阅核对的是本地对象与已保存的remote-tracking refs，没有重新联网确认远端当前tip；正式执行Git方案前仍须按计划重新读取远端并使用精确lease。

## 4. Git方案与本轮边界

Git方案符合现行治理先行原则：共享治理从`a057d26`续接，开发内容从实际`2e1cbce`继续，通过明确区间重放改变父链；没有建议把治理线前移到开发tip，也没有执行重排。其225提交授权明确与ADR审批分开，要求先远端保全原链、保护脏工作树、range-diff/tree核对、精确lease和推送后复证。无需为了本方案修改repository-safety。

修复F1–F3后可做一次有界复审；本结论不批准治理采纳、受保护历史重排、正式R/D/T写入或生产实施，不签发RPY6.2/6.3或跨平台交付PASS。此次仅写本报告，未运行产品测试，未改变生产、规格或其他Agent内容。
