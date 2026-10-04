# Fuzzy 性能中段评审：准入、轮询生命周期与最小补证

日期：2026-10-04。交接基点：`temp-fuzzy-performance-review@f5741a0d7f5d2ec866366109a9d0a21c6869535a`；代码基线：`7b51cce583dbeb32d35047abbee614bbc609f457`。

本文续接 [HANDOFF-FUZZY-PERFORMANCE.md](HANDOFF-FUZZY-PERFORMANCE.md)，记录静态核查、产品目标澄清和下一轮最小补证要求。数值依据现有 handoff 与匿名摘录；本轮未执行 Windows 产品、Gate 或性能测试，未取得原始环境 envelope、执行日志及设备资格来源原件。本文不是缺陷修复、正式设计、实施授权或新的验收，不修改原报告、审批、任务勾选或历史 PASS/FAIL。新增生命周期问题的实际发生情况来自 owner 转述，下面将代码机制与尚缺的运行证据分开。

## 1. 产品目标澄清：主体功能开箱即用，Fuzzy 可以等待验证

owner 本轮明确：追求无需用户安装 Python/Qt 即可使用主体功能；接受 Fuzzy 在软件启动后等待验证或由用户手动发起验证。此前外部评审把这一目标扩大为“Fuzzy 第一次必须免验证立即可用”，不是当前产品要求，撤回该扩展，不据此提出取消本机完整验证的新前置任务。

以下三件事不同：何时开始验证、验证时界面能否继续使用、何时发布 Fuzzy 能力。允许启动后验证，不等于允许验证未完成就先开放未验证的 Fuzzy。主体编辑、保存及各自条件成立的 Exact/Context 不应被完整性能验证等待阻塞；它们自身的数据和语义检查仍然保留。

[ADR-013](.kiro/steering/adr/adr-013.md) 当前规定：已有兼容且可验证的本机资格在启动后恢复；没有资格或资格失配时不自动运行 100k，由用户显式重验。早期后台自动验证与目前的手动重验是历史演进，不应混写成当前自动重跑规则。本轮最小政策只改变时间指标对准入的作用，不顺带修改验证触发方式。若之后明确选择首用自动后台验证，应单独、精确修订触发条款，而非从本次目标澄清推导出默认行为。

因此最小方案与当前目标不冲突：主体功能可用 → 已有资格则恢复，否则等待显式验证 → 验证真实完成后由 Core 判定；仅时间指标失败可开放 Fuzzy 并如实显示性能不足。日常建议可以晚到，但必须不阻塞编辑、不展示错段结果、不自动应用译文。

## 2. 优先级裁决

| 优先级 | 事项 | 本轮结论与证据限度 |
| --- | --- | --- |
| P0 | UI 轮询状态遇后台锁争用抛异常 | 源码存在吻合的确定性抛错机制；先补完整堆栈和事件时序确认实例，不归入时间超限政策，不以吞异常或取消全部锁保护解决 |
| P1 | 时间指标与功能准入解耦、日常建议非阻塞 | 认可 handoff 最小规则；准入与展示/恢复必须一致。状态轮询与日常查询异步是两条不同链，分别闭合 |
| P1 | 两机查询差异与 Phase 1 工作量 | 约 1.65 倍差异支持优先检查公共路径及运行条件，不支持指定某硬件或参数为根因；先补已存在的对照事实，再做有限分阶段实验 |
| P2 | Phase 1 结构优化 | 可评估全域保守上界加按需精确交集；必须修改对应事实/receipt 解释并证明守恒，收益以总成本判断，不默认多核化 |
| 独立收束 | 发行和文档事实 | 性能报告、功能许可、最终发行结论分开；旧图/旧候选称谓的修正不增加产品依赖，不代替上述能力工作 |

当前不建议等待所有原件齐全才记录静态结论，也不建议先做完整硬件测评。先提交本文，再由 agent 按第 7 节补一次有针对性的证据；没有测量过的字段如实标为未采集，不从旧摘要补造。

## 3. 各道门在检查什么，以及失败到底阻止什么

“硬门”必须说明阻止对象。报告 FAIL、关闭某项能力、拒绝当前资源/请求、停止一次验证、拒绝整个发行，不是同一个动作，更不统一意味着退出软件。

| 检查 | 目的 | 保留的阻断范围 |
| --- | --- | --- |
| Gate A / Matcher 基础验证 | 检查合同、基础评分/文本匹配及黄金样例，避免错误匹配规则成为正式能力 | 对应组件不授权或按既有规则降级；不是以文本匹配失败撤销所有编辑/保存 |
| Gate B / 数据激活前检查 | 判断迁移产物是否完整、与已封存来源相符，能否进入后续激活 | 不能证明就不激活该候选数据；已有数据按 owner 的保留/恢复协议处理。Gate B 本身不发布 generation |
| Gate C：Context 与 Fuzzy 正确性 | 检查已知向量、评分/排序、存储/服务旅程、快照和结果语义 | 对应子门失效就不授权该能力；Fuzzy correctness 失败不能靠“只是慢”豁免 |
| Gate D：实际路径 oracle/召回 | 与全扫描真值对照，检查阈值以上集合及真实 top-10 是否漏项、实际路径是否一致 | 召回或路径不成立继续阻断；现有 runner 在两路 oracle 未全部满足时不进入后续完整 100k migration/query |
| Gate D：耗时和 RSS | 衡量固定 100k 负载的速度和资源占用 | 报告仍按原指标失败；最小方案仅将三项时间失败从功能一票否决中移除，RSS 暂留现行准入限制 |
| 执行/证据来源与恢复 | 保证报告不是任意 PASS JSON，属于真实执行、当前实现及可验证本机资格 | 缺失、伪造、损坏、路径/指纹/环境/兼容性不符或无法复证，不授予相关资格 |
| 每次查询的预算/完整性/生命周期 | 防止展示漏项的结果、跨代次拼接、取消后的迟到结果或故障资源结果 | 拒绝受影响请求/资源的 Fuzzy 结果并按原合同报告；不把一次请求超时改成永久整机禁用 |
| 最终发行验收 | 判断该候选是否满足承诺的产品旅程、数据保护、环境与性能标准 | 不满足仍不可记为最终发行 PASS；某台机器可用不能覆盖另一环境的失败 |

核查入口：[Gate A](tm_gate_a.py)、[Gate B](tm_gate_b.py)、[Gate C](tm_retrieval_validation.py)、[Gate D runner/发布/恢复](tm_benchmark_gate.py)、[唯一能力判定](tm_retrieval_capability.py)、[Core Design](.kiro/specs/tm-storage-retrieval-index/design.md)、[Core Requirements](.kiro/specs/tm-storage-retrieval-index/requirements.md)。这不是新增 Gate 清单，而是解释现有职责。

四个数值都属于 Gate D 的性能验收，不是 Gate C 的通过条件：

| 数值 | 测量对象 | 不能误解成 |
| --- | --- | --- |
| 50 ms | 固定 100k 语料、预热后的 Exact p95 | 所有用户操作必须在 50 ms 内完成 |
| 500 ms | 同一基准的 Fuzzy top-10 p95 | 单条查询到 500 ms 必须杀掉进程 |
| 120 s | 100k 基准迁移耗时 | 真实迁移过 120 s 就可以丢弃半成品、跳过恢复或必须立即退出 |
| 512 MiB | 原 benchmark 规定的 migration/query child lifetime 峰值 RSS 口径 | 整个 GUI 的统一内存配额或一定已经发生错误检索 |

指标比较通常决定报告与资格；独立 worker 的执行 timeout、异常退出和取消是另一些执行结果。完成用了 148 秒与 120 秒时被终止、没有完整结果不是同一个状态；最小政策只允许前者作为时间超限处理。RSS 也是资源目标，不等于超过就数学上不正确；本轮为控制范围而保留现行阻断，不把它论证成不可永久调整的语义定律。

## 4. 准入最小规则及精确合同影响

建议以 Core 对完整、严格、来源有效、兼容的本机报告的重新判定为准。只有 `EXACT_P95`、`FUZZY_P95`、`MIGRATION` 可以作为不单独否决功能的时间失败；Gate C、真实路径 oracle/召回、RSS、工件/来源/兼容、完整执行结果和原发布协议仍须满足。未知失败项默认拒绝；不能只从 caller 提供的 failed_gates 列表判断，也不能只检查“不是召回失败”。

报告 `passed=false`、原始样本、50/500 ms、120 s、512 MiB 和失败项均不改。以 VAL handoff 为例，FTS5 的 FUZZY_P95 失败和 fallback 的 FUZZY_P95/MIGRATION 失败，在其余必要事实全部闭合且新政策获批实施后可以允许功能；这不是本次已经开放，也不是将 VAL 改记为性能通过。

静态核对发现政策并非只在 ADR-013 一处：

- `tm_retrieval_capability._fuzzy_core_decision` 核验 Gate C cohort、输入身份和有效窗口；此正确性硬门保留。
- `tm_retrieval_capability._path_decision` 当前还要求 `report.passed is True`、`report.failed_gates == ()`，以及 path、contract 和有效窗口；最小规则必须由唯一 evaluator 精确取代其中的时间全通过依赖。
- `tm_benchmark_gate` 的 `_report_path_truth`、路径决定/报告一致性检查与 publication 也按全项通过解释；须与 evaluator 的新准入含义同步，不能让一端放行、另一端拒绝。真实 runner/receipt、report validator、attestation 签发与 restore 需要同一解释。
- `tm_contracts` 中 BenchmarkReport/BenchmarkSuiteReport 对 measured verdict 的严格重算仍保留；不可为了可用性把报告的 passed 改成另一层含义。
- 准入政策须被现有 evaluator/compatibility 身份覆盖；只有发现实际覆盖不足才补精确字段，不新增 profile registry。旧失败文件不能由 UI 自动升级；如要复用旧执行事实，由 Core 证明其来源与当前兼容并作新的可追溯判定，原测量结论不变。仅凭本次 CSV 或脱敏摘要无法完成这种授权。

治理按现有共享治理 tip 规则精确取代 ADR-013 相关准入/签发/恢复解释，继续遵守 ADR-009 的唯一 Core authority；Core R8 保留数值与“超限报告失败”，补充独立准入规则，更新 capability/Gate D/恢复 Design 和对应任务。Feature5/Qt 安全投影区分验证进度、能力可用与性能结果；平台 R12 分别引用产品可用和性能 verdict。验证触发方式、取消本机 100k、放宽 RSS、改变召回与最终发行标准均不由本条顺带授权。

## 5. 新生命周期问题：代码机制成立，现场因果待堆栈确认

owner 转述：候选日志出现 UI 轮询 Fuzzy 验证状态时，碰到后台发布锁而抛异常。当前没有取得该日志，不能声称已确认具体抛错栈、持锁范围、进程崩溃或数据损坏。

在 `7b51cce` 中已能核对以下链路：

1. [qt_editor.py](qt_editor.py) 的 `_fuzzy_validation_display()` 调用 Gate D owner 的 `status()`，并将其投影为 UI 状态。
2. [capability_host.py](capability_host.py) 的 owner `status()` 使用 `with self.__condition`；condition 由 `execution.condition_lock()` 建立。
3. `condition_lock()` 对普通 packaged 输入也可能返回 `_OwnerWaitGuardLock`；Host 的部分 snapshot/notification 读锁也绑定此 guard。
4. [capability_frozen_inputs.py](capability_frozen_inputs.py) 的 `_OwnerWaitGuardLock.acquire()` 先做一次非阻塞尝试，失败后调用输入来源的 `require_worker_wait_allowed()`。
5. `_OrdinaryCapabilityInputs.require_worker_wait_allowed()` 在主线程直接抛 `RuntimeError("ordinary worker wait cannot block the UI thread")`。

所以，只要主线程读取恰好遇到另一个线程持有此 guarded lock，就有吻合的抛错路径，不要求先证明算法变慢。这是项目自定义“UI 不准等待”策略与只读观察路径之间的接缝，不是 Python 的普通 RLock 在争用时自然判错。是否正是候选日志中的异常、争用的是状态 condition 还是另一个 Host 观察/发布锁，仍应由现场堆栈确认。

修复边界建议：只读 UI 轮询应能取得最近一个完整的、带原有 epoch/generation 的安全投影，或在短暂争用时保留当前显示并稍后刷新；不得因正常争用报告验证失败、签发新能力或阻塞等待长操作。最终状态应在后台完成后有保证地通知/刷新，不能把异常吞掉后永远停在 RUNNING。实际 capability 仍由原 owner 的提交决定，旧显示不能用于授权。

不建议立即全局替换 guard 为普通 Lock、增加无限等待、取消 publication 原子性或加 `except Exception: pass`。先确认持锁范围，再在 owning 生命周期中收窄状态观察和发布准备的耦合；如需要独立短锁/已提交状态快照，必须保持代次一致及关闭语义。这是现有生命周期缺陷的修复候选，不因涉及锁就新立 ADR。

最小复现实验：用受控 barrier 让后台停在日志确认的持锁点，由 UI 连续触发状态轮询，然后释放后台；覆盖发布成功、真实性能失败、取消和关闭。记录主线程是否有未处理异常/长等待、状态是否最终可见、取消胜出是否零安装、合法 commit 先胜出是否完整保留。测试夹具只用于制造时序，不得授予 Gate PASS；修复后的真实候选仍须验证对应生命周期。

该异常不会自动推翻已完整保存的逐查询样本，也不能据此认定原产品旅程仍通过。需核对异常相对 oracle、worker 退出、报告持久化和资格提交的时序：如果只影响显示，标明受影响的 UI 证据；如果触发取消、错误终态或不完整保存，则隔离受影响 run。不要一律作废全部历史，也不要拼接不同 run 的成功片段。

## 6. 性能与日常异步的中段结论

现有 CANDIDATE 摘录显示两路 VAL/DEV 配对中位比约 1.65–1.67，near-edit 和 miss 均普遍变慢；8 个 source 插桩查询走 dense、检查 100k identity，但真实 scorer 只调用 19–42 次。插桩本身明显增加总耗时，frontier/projection 累计时间嵌套，不能相加或把 source 占比当作两机 packaged 因果对照。HighQoS 的限定实验没有足够收益，不再列为主方向，也不据此排除频率、热状态、内存、缓存或存储。

当前 Phase 1 的主要工作在全域长度读取、query bigram postings 聚合、JSON 传输/解码、重复类型/绑定验证、上界分组和 P1/R 分区。应分开 identity 数、postings 行数、scorer 次数与阶段 CPU/墙钟。两机同输入不保证物理 DB 字节、SQL plan、缓存和系统负载相同；没有记录的事实仍是未知。

“先取得所有记录的精确 bigram 交集”是 CandidateRetriever 的具体设计，不是完整召回的唯一实现。可评估便宜保守上界后按需精确化：若已处理 query gram 子集 H，p_H 是对应精确交集、R_q 是未处理 query gram 总频次，则 `I <= min(Bq, Br, p_H + R_q)`。未出现在 H postings 的记录仅可取 p_H=0，不能当作全交集为零。必须从真实 scorer 上界论证安全，不能用启发式相似度排除。

这只是可验证假设：若上界过松仍读取近全量 postings，额外扫描/验证/内存抵消收益，就停止。不能把 upper 值塞进现有 exact-intersection receipt。修改必须落在 Core 私有 facts/receipt、frontier、partition 与独立复算合同，必要时精确版本化；scorer/fold、raw-distinct identity、tie、budget、snapshot/head、数据保护不变。Production 的 result-complete 与 Oracle 的 threshold+top-k 双闭合仍按现有不同规则执行；全量遍历不等于全量 scorer，多核不是默认解法。

日常建议另有 [qt_editor_window.py](qt_editor_window.py) `refresh_suggestions()` → [editor_controller.py](editor_controller.py) `tm_suggestion_report()` 的同步接缝；后者在 `_tm_query_lock` 中查询，切段等也消费同一锁。只把原函数搬到 worker 仍可能阻塞 UI。建议在既有 owner 内组织短锁捕获请求 → 锁外执行 → 短锁核对提交 → UI 渲染，复用 query epoch、项目/文档/段身份、参数与 runtime/retrieval generation。一个执行中请求加一个可替换的最新待执行请求，避免快速切段堆积过时工作；取消还须释放执行资源，而不只是丢弃结果。

最小交互可以等待整份有效 TM 报告完成后替换面板；术语等独立内容无需等待。若要 Exact/Context 先显示、Fuzzy 后显示，应明确阶段结果与快照/排序/global-limit/失败语义，不能把不同请求或快照拼成一份报告。A→B→A、修改阈值/资源、关闭、晚到结果和应用时 membership/代次核对必须覆盖。后台 Gate 异步、状态轮询无异常、日常查询异步是三个需要分别证明的事实。

## 7. Agent 下一轮只需补什么

先提取已存在材料，不重跑完整 Gate，不修改原日志/报告或上传秘密。建议在现有 `review/fuzzy-performance/` 中提供一份简短补证说明和必要摘录即可，不增加多套 ledger/归档。补证本身也不是来源或性能重新验收。

| 次序 | 最小材料 | 能补足的判断 |
| --- | --- | --- |
| A：轮询异常 | 脱敏完整 traceback（相对模块/函数/行号、异常类型和原消息）、代码 SHA/候选 ID、触发动作、线程角色、异常前后状态/epoch；同一次 run 的 oracle/child 退出、报告落盘、资格提交与取消/关闭时间顺序 | 确认 guarded-lock 机制是否对应现场；区分显示异常、能力发布失败与实际执行中断 |
| B：两机可比条件 | 同一 run 的候选/fingerprint/contract/path/corpus/query 标识；包内 runtime、SQLite compile options 与实际相关 pragma、DB 逻辑与已记录的物理摘要、query plan、运行先后/预热/缓存处理、供电/负载与已有阶段 CPU/墙钟/I/O 事实 | 确定是否同一工作量、何种已知运行条件不同；没有阶段测量就不能从 envelope 单独推断根因 |
| C：本地证据来源核查 | agent 在原机器按 Core 正式 validator/restore 检查的命令、退出码和拒绝原因；strict schema/digest/兼容性与 HMAC 校验结果、报告失败项、两路 oracle/worker 完整性、资格是否实际签发/恢复的事实 | 核实原件的归属、完整性和恢复结果，不等价于共享件可在其他机器授权；不解释 CPU/SQL 为何慢 |

不提供设备私有密钥、凭据、完整环境变量、用户项目/TMX 正文、未脱敏用户名/路径/主机名/地址。HMAC 密钥留在本机；外援取得的本地校验摘要是 agent 的执行记录，不冒称外援独立验证了秘密。原件本地保留，分享件记录与原报告的关联和脱敏范围，不能覆盖原证据或变成产品恢复 bundle。

环境材料如确需 CPU 拓扑、内存规模、活跃核心/频率等，只提供回答已定位假设所需的匿名事实，不因为“完整 envelope”而收集所有硬件型号或秘密。旧日志没有测过的阶段、频率、SQL plan、缓存状态，明确标未采集；新增实验另行固定输入、范围和候选身份。

若补证后仍不能解释两机差异，下一有限实验是两机同一组查询的工作量与阶段对照：先用 8 个已有 profile 查询，加少量低成本与 miss 对照，测 SQL、JSON/验证、frontier、scorer 和总耗时；先不改索引、电源计划、杀毒或重建数据库。只有差距集中在 SQL 且现有事实不能解释时，再考虑隔离合成 DB 的交叉对照，不复制设备资格或替换用户数据。结果仍为诊断，不重签正式样本。

## 8. 收束与后续范围

先核查并定位轮询生命周期缺陷；准入和异步建议按明确 owner 收束为完整能力；性能因果与 Phase 1 优化各自有有限实验和退出条件。正式数值不放宽，数据/语义/来源不豁免；新政策不自动批准最终发行，也不取消本机验证。

下一份 agent 汇报只需回答：轮询异常的确定触发链与影响范围是什么，现有材料已排除/仍不能区分哪些两机差异假设，以及最小政策具体会改哪些判定/签发/恢复/展示接缝。没有取得运行证据的地方继续标未验证。实现和新候选验收仍按原 owner 的批准流程进行，本静态 review 不授予额外修改范围。
