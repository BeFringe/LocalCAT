# Windows 普通 frozen 复核意见与后续范围

日期：2026-09-20。评审基点：`reassessment@58292d7c667520dbc4782e29ba0579a23957fa7f`，即 [HANDOFF-WINDOWS-FROZEN.md](HANDOFF-WINDOWS-FROZEN.md) 的交接提交。

本文按项目 owner 的更正，续接 `reassessment` 的 handoff，而不是提交到 continuation。它只记录评审意见、owner 补充的过程事实和后续规格修订建议；不修改生产代码、正式 R/D/T、Steering、任务勾选或审批状态，不签发 frozen/Gate PASS。方向上的认同不替代具体消费设计的审批。本轮为文档与代码静态复核，未运行 Windows 测试，未取得本地 `artifacts/windows/` 原始实验材料。

## 1. 转向标记保留，有限解除依赖不等于重排 Git

认同从强证明 W3 转向普通 PyInstaller onedir/windowed。这里的“从现有修复基线做有限的依赖解除”是代码与消费合同的后续工作范围，不是建议取消单次转向提交，更不是要求退回 spike 重新抽取成果。

已有提交关系与本文位置如下；后续设计和实现不属于本次 review 提交：

```text
6531b1e  continuation 整理后的修复基线
   |
0dfe833  reassessment 转向标记：普通 frozen 边界与文档调整
   |
58292d7  HANDOFF-WINDOWS-FROZEN.md
   |
本 review：只增加评审文档
   |
后续：经批准的规格修订与有限实现任务
```

`0dfe833e41209b20f504f33cd137136083b2a51c` 继续是原转向标记，不因随后增加 review、设计或代码提交而失去作用。单一转向节点与若干后续能力提交并不冲突；也不应为了保持“单次转向”而将所有未来实现压进该节点。本文不要求 rebase、squash、force-push、重新签发历史验收或同步其他本地分支。

按 owner 本轮补充，Windows source、轻量 launcher 和业务旅程在 spike 前已完成；随后为 Task 7–10 的完整 frozen 消费追加 WA-06 R4，补齐 Core 9.6c/9.6d、WA-07 3.6a 等前置。在平台 7.2、Core 9.6b 的返工过程中，owner 叫停并要求 step back/reassessment。叫停位置属于 owner 补充的过程事实，不代表这两项已验收。

需要区分 WA-06 的总体 Windows Core 修订范围与 R4 这一轮的增量：WA-06 涉及 TM 数据、权限、发布恢复、检索与 frozen 消费；[R4 合同](.kiro/specs/windows-platform-enablement/task7-prebuild-consumption-amendment.md) 的本轮重点是受信输入、fresh migration/query worker 和相邻消费分工，沿用既有 Requirements 语义，Core 9.6b 仍承担同候选的 post-build 重放。不能把 R4 写成再次完成 source 迁移，也不能把构建前完成勾选解释为完整 frozen PASS。

因此当前要解决的是：普通 packaged 入口、Core 输入来源、Host composition 和 same-EXE worker 如何摆脱 W3 专属 native/source-only 前置，同时保持已有业务与生命周期不变量。Windows 数据端口、锁、发布恢复、检索算法和已修复的发布/撤销行为，不作为重新开发的默认对象。继承代码不等于必须继续调用其中所有 W3 机制；旧研究实现可以留在历史或隔离位置，但不得被普通发行路径隐式重新激活。

## 2. 先收束活动规格，不靠总则覆盖大量旧正文

[ADR-028](.kiro/steering/adr/adr-028.md) 已批准产品边界，但普通 frozen 的替代消费设计尚未完成。[spec.json](.kiro/specs/windows-platform-enablement/spec.json) 的 `ready_for_implementation: false` 与 `ADR-028_REDESIGN_PENDING` 应保持，直到对应正式阶段完成审批。本文不提前改成 GO。

同意下一步集中修订以下活动内容，而不是再增加一层概念总则：

| 位置 | 必须完成的修订 | 不应顺带扩张的范围 |
| --- | --- | --- |
| Windows Requirement 10 及对应 Design/Tasks | 从外置 `.py`、TrustedSourceLoader、native entry、完整 Boot TCB 改为普通 packaged 输入与执行产物的可追溯绑定、真实 Core 消费及失败行为；明确不伪装 source/native authority | 不重建第三方逐事件执行证明，不改检索算法、阈值或数据保护合同 |
| Requirement 11 及构建/资源设计 | 替换不再适用的 frozen-source/闭包来源要求；保留 onedir/windowed、自包含、必要 Qt/SQLite/业务资源、非仓库 CWD、正常首页/项目参数和用户目录语义 | 不新增 onefile、安装器、签名、自动更新、macOS frozen 或新业务功能 |
| Requirement 12 及验收任务 | 明确候选必测、可引用未改变 owner 证据、由实际变更触发重验三种范围；每项说明失败现象和证据来源 | 不默认每次 UI/头像变化重跑整仓全部平台与全部历史矩阵；也不把必要权限、并发和恢复反例当 W3 废项删除 |
| 相邻 owning Design/Tasks 与现有 amendment 合同 | 只修正受影响的输入、worker、Host、qualification 消费接缝和旧前置引用；保留原完成事实的范围 | 不另建一套业务 authority，不为没有公共合同变化的实现细节制造新修订编号 |

[现有 Requirements](.kiro/specs/windows-platform-enablement/requirements.md) 和 [Tasks](.kiro/specs/windows-platform-enablement/tasks.md) 中的历史条款可以追溯，但不能同时充当普通发行的活动指令。既有阻塞项的删减或证据复用必须在 owning 规格中明确批准，不以执行时写一个 SKIP 代替范围裁决。

三类复盘继续作为判断依据：用户数据保护保留 rooted/reparse/live identity、锁、发布恢复和私有 SID/ACL/MIC；检索保留真实 Matcher、Gate C/D、oracle、正式性能门和设备资格；程序来源则按普通桌面信任范围收窄。移除 custom native entry 不等于移除 Windows 数据访问所需的原生 API。保留某类保证也不等于永久保留其每一种既有实现形式。

## 3. 三类身份是设计中的作用域，不是三套 Steering 制度

候选身份、检索兼容身份、单次运行身份是为解决不同生命周期问题而作的设计区分；它们不是三类安全复盘的另一套命名，也不要求三个新 Spec、三个新 authority 或三份独立 Steering 文件。

[AGENTS.md](AGENTS.md) 与 [spec-ownership](.kiro/steering/spec-ownership.md) 已区分项目级长期规则和功能级 R/D/T。建议在 Windows Design 中用一节表格说明整体关系，具体字段、生成方、校验方、传播和失效行为回到各自 owning Design；消费者引用唯一合同，不复制定义。

| 身份作用域 | 回答的问题与消费者 | 建议落点与边界 |
| --- | --- | --- |
| 发行候选身份 | 本次运行、parent/worker 配合和最终验收对应哪个实际发行物 | Windows packaging/entry/transport Design 拥有产物标识与一致性检查；Core 消费必要的关联事实。候选 ID 不是 Gate PASS，也不直接等于持久 Fuzzy 资格 key |
| 检索兼容身份 | 本设备此前完成的 Gate D 资格是否仍适用于当前检索实现与运行环境 | TM Core 的 fingerprint/qualification Design，受 [ADR-013](.kiro/steering/adr/adr-013.md) 约束；平台提供实际构建/运行时输入，不能自行决定 Fuzzy 授权 |
| 单次运行身份与 generation | 结果是否属于当前请求、session/epoch 与尚可提交的生命周期 | Core 输入、worker request/result、publication 合同与 WA-07 Host 调度 Design 分别承担既有职责。优先复用现有 session、epoch、generation；如确有跨进程关联缺口，再补最小关联字段，不默认新增持久化 run-attestation 系统 |

Steering/ADR 只保留长期边界，例如 Core 是资格发布 owner、不同作用域不能混用、普通发行信任范围、无关 UI 改动不应使检索资格无条件失效。现有 ADR-013/028 已能承载这些原则时，无需新立 ADR。只有确实改变长期承诺、owner 或公共合同，才走对应治理流程；本 review 不在功能线上直接修改 Steering。

持久资格 key 应覆盖实际影响语义或性能的检索实现、Gate/fixture/proof、相关运行时和 intended path。影响这些结果的构建转换、worker 执行或测量机制变化也须纳入对应身份；“key 更窄”不能变成漏依赖。完整 EXE/PYZ 摘要可用于候选一致性，但不得无差别作为检索资格 key，使头像或纯 UI 改动必然触发 100k 重验。

无有效本机资格时保持 Fuzzy fail-closed，Exact/Context 保留各自能力；启动不自动跑 100k，由用户显式重验。数据/项目交换不携带 Fuzzy 授权，source/W3/普通 packaged 的证据不能未经兼容性与来源验证而互认。

## 4. 认同的最小路线，以及应收窄的机制

认同普通 PyInstaller onedir/windowed、same-EXE 独立 worker、复用 Host 发布生命周期和区分候选/资格身份。以下建议是在该方向内收窄设计，不是先实现另一套 W3 才允许真实业务运行。

**受控构建与实际执行绑定。** 保留 clean tracked 输入、固定依赖、owner 声明、构建配方、实际产物清单/摘要及同候选运行证据。构建应能说明 owner 输入进入了哪个实际产物，并排除意外的 checkout、外部 venv、CWD 或未声明依赖消费。构建记录只建立发行可追溯关系，不凭其自身铸造 Gate capability；Gate 仍由真实 Core 执行并按原发布协议产生资格。

不建议把“所有 PYZ code object 与重新 compile 的源码逐字段相等”列为普通发行的默认硬门。小程序的常量修改负控并不证明完整 LocalCAT 的代码变换与运行时已覆盖。如果提出这项额外比较，应先说明它补足受控构建和同候选验收的哪项具体风险，以及为何与 ADR-028 的信任范围相称；不能只因旧 source-only 方案要求逐字证明而继承。

ADR-028 第 6 条的“绑定实际执行产物”建议在后续设计中明确为受控构建的输入—产物对应关系加真实候选消费，不自动解释为逐模块运行时自证。若现有措辞确被解释为必须承担后者，则提出该条的精确治理澄清，不默默绕过，也不为普通实现另造新 ADR 体系。

**`proof-inputs`。** 区分 Gate 真正需要的 fixture/contract/owner 数据与仅为旧 fingerprint 接口携带的源码副本。前者是正常随包输入；后者最多是受控构建绑定的、范围有限的兼容数据，不参与 import，不提供执行 authority，不继续触发 native reproof 或完整 AST 图重建。旁置未执行源码本身不足以证明真实执行。优先让 fingerprint/输入消费接口不再要求外置源码执行形态；如少量只读兼容数据明显比广泛重构更小，可以评审后保留，但需列明实际消费者和用途，不以消灭所有副本为由重写 Host。

**新的 profile/context。** source 与 packaged 可以有明确的组合分支或内部输入提供者；不要求将其提升为新的可扩展 profile 框架。普通上下文只承担必要的候选/兼容信息、有限输入读取或快照以及撤销关联，不能另行签发 capability，也不能仅包裹旧 native authority 后继续承担同一套证明。

**Host 与 Core 接缝。** 保留现有 Gate runner、能力发布 owner、generation/通知和取消/提交协议，只替换与普通 packaged 不相容的输入身份/组合机制。按 handoff 指出的生产调用链贯通 oracle 与 Gate D 的 session；将 query 证据的纯一致性比较与当前运行时身份验证分开。普通结果 DTO 不持有活 authority，也不在构造时回读 ambient source fingerprint。不复制整个 `capability_host.py`，不让平台成为第二个业务授权者。

**worker 与取消。** same-EXE child 使用限定模式和显式二进制管道，消费 Core 的严格 codec、超时、退出/错误和原 RSS 口径；没有外部 venv 或进程内 fallback。parent 管理自己创建的进程和端点，取消需闭合到停止/等待退出/句柄回收，Qt 不阻塞 join。撤销 publication 与回收 child 是两项义务，不能互相代替。

保留 [Core publication 合同](.kiro/specs/tm-storage-retrieval-index/trusted-input-publication.md) 的竞态语义：取消先赢则零安装；合法 commit 先赢则完整安装并记录完成，不能事后将其倒判成未发生。所谓“取消后无晚到发布”是禁止取消胜出后由过期运行继续授权，不是追溯否认已完成提交。

windowed 标准流不可用、管道缺失/截断、畸形结果、超时和 child 异常退出需要真实覆盖。handoff 中小程序的 stdin/stdout 均非 None，不能沿用为 None 场景的通过证据。性能定位可分阶段记录启动、输入处理、迁移、oracle、查询、发布和退出成本，但正式 Gate 不得通过挪动计时/RSS 边界制造 PASS；[benchmark contract](benchmark_tm_contract.json) 的正式门限保持不变。

## 5. 用少量完整能力任务推进同一个 0.5.2

最小依赖顺序：活动 R/D/T 收束并批准 → 普通 EXE 的首条真实生产调用链 → 本机 Fuzzy 资格闭环 → 冻结候选的完整产品验收。以下是重写现有任务的建议分组，不是新 Task 编号、独立发行版本或新的审批制度。

| 能力任务 | 应交付的可观察结果 | 完成条件 |
| --- | --- | --- |
| 首条真实 packaged 调用链 | 正常入口、用户目录和资源；真实 Matcher/Core 输入消费；same-EXE migration/query；结果消费、取消与退出 | 无 checkout/外部 venv 依赖；没有 mock authority 或伪造 PASS；真实消费者贯通，取消后过期结果不能发布，自己创建的 child 可回收。小样本只用于集成诊断，不签发正式 Gate D 资格 |
| 本机 Fuzzy 资格闭环 | 实际 Gate C/D、正式 oracle/100k intended paths、设备资格发布、重启恢复、失配及显式重验 | 原门限、算法和计量口径不变；有效资格可恢复；失效只关闭 Fuzzy；启动不自动 100k；取消/失败不会由 UI 展示状态或自洽 JSON 代替 Core 授权 |
| 同候选产品验收 | 完整 Qt/Project/TM/TMX/FTS5 旅程、必要数据保护和运行生命周期 | 所有候选必测项落在同一最终产物；每项复用证据有明确不变前提；任一阻塞项失败则不宣布 0.5.2 完成 |

第一项任务就应经过真实 Core 和 worker，不能再次先把 producer、profile、清单生成和 loader 各自做完，最后才发现实际消费者无法运行。机制实验是该调用链的诊断工具，不是产品交付，也不扩大首发承诺。

验收范围建议如下，正式落地仍须进入 Requirement 12 与相邻 owner 的相应任务：

| 证据范围 | 具体内容与失效条件 |
| --- | --- |
| 必须落在实际候选上 | 非仓库 CWD、干净用户/无开发环境依赖、真实首页/项目参数；项目编辑保存重开；TM 激活与重启恢复；TMX 直接导入；FTS5/trigram 创建查询重开；真实建议消费；正式 C/D；资格恢复/失配；worker 取消、关闭、超时及退出；新增/改变的打包资源与加载路径 |
| 候选中的数据与边界反例 | 默认资源仅缺失时播种，不覆盖用户配置/数据；实际调用链的锁竞争、发布失败、中断恢复、权限/reparse 拒绝；失败不损坏既有用户数据。TMX ResourcePackage 继续 export-only，import/apply 负向拒绝；资源迁移不携带本机 Fuzzy 资格 |
| 可引用未改变的 owner 证据 | 数据端口与 owner 状态机的既有深入单测/故障矩阵，前提是实现、依赖、调用合同和关键运行条件未改变，且候选实际消费已验证；不得用 source PASS 替代 packaged 入口、同 EXE worker、Qt 生命周期和运行时特性的验证 |
| 随变更触发重验 | Core/索引/Gate/相关运行时或计量变化触发对应资格与性能验证；共享数据/权限/发布端口变化触发对应 Windows 安全恢复矩阵；共享代码变化触发受影响的 source/macOS/Linux 回归。纯 UI/头像变更重验受影响功能与资源路径，不无条件使检索资格失效 |

同候选原则与证据复用不矛盾：可复用的是未改变底层机制的证明，不是把不同 EXE 上的集成成功片段拼成最终 PASS。若后续修复改变候选，应更新候选身份并按影响范围重验，最后仍需完整候选的集成结论。不能一边宣称采用 stock 机制，一边将旧 native/第三方对抗实验全部重新列成必测。

## 6. 进度检查与范围增加的约束

认同以如下三个问题作为后续进度检查的主体：

1. 新增贯通了哪条真实能力？给出实际入口、消费者、观察结果及证据范围。
2. 当前实际阻塞是什么？区分数据或生命周期缺陷、真实集成缺口、性能瓶颈与超出普通发行承诺的证明负担。
3. 下一次实验要消除哪个不确定性？给出明确现象和失败条件，不以“继续完善所有证明”为目标。

测试数量、amendment 数量、报告篇幅和局部组件通过数只作为支持信息，不替代产品能力进度。完成定义是经批准范围内的真实消费者贯通、正常/失败生命周期闭合及所需证据成立，不是能构造 adapter、启动空窗口或生成构建目录。

新增运行时证明前应说明：保护哪个用户结果、覆盖什么具体风险、为什么不能在受控构建或发行验收完成。只有实际发现的消费缺口、数据损坏/越权风险、child 泄漏/死锁、正式性能不满足，或需改变公开合同/长期承诺时，才按问题归属调整范围；不是每个开放问题都升级为 ADR。必要修复可以开展，但应有有限的复现实验和退出条件，不默认扩展到整个第三方运行时。

本次方向调整不降低现有正式性能门、必要安全测试、独立 review 或既定审查强度，也不以重新整理历史与材料替代产品进展。0.5.2 仍只有一个完整交付目标：用户无需安装 Python/Qt，在同一普通 frozen 候选上使用既有产品能力并满足保留的业务与数据保证。

结论：保留单次 Git 转向节点，把三类身份放回既有 owning Design，先修清活动规格，再以真实调用链驱动有限实现。该停止扩张的是超出普通发行承诺的证明系统；该继续闭合的是用户数据、真实检索、worker 生命周期和同候选验收。
