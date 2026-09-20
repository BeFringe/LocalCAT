# Windows frozen 转向评审：Git 标记、设计归属与交付退出条件

日期：2026-09-20。

评审对象：`reassessment@58292d7c667520dbc4782e29ba0579a23957fa7f` 的 `HANDOFF-WINDOWS-FROZEN.md`、相邻合同及本轮项目 owner 的补充说明。承载位置：按 owner 要求，在 `continuation` 追加本 review；其父基点为 `6531b1e334ed99993aa74f887f3a1c24f775345e`。

**性质：路线与设计评审意见，不是任务实现验收、新 ADR、正式替代设计或实施授权。** 本次只新增本文，不修改 Requirements/Design/Tasks、Steering、代码、任务勾选或审批状态，不宣称重新运行 Windows、native、Qt 或 Gate 测试。已确认的产品边界仍以 ADR-028 为准；下文具体接口、字段和任务组织是供 owning Spec 收束的建议。

## 1. 先纠正歧义：有限解除依赖不等于取消单次 Git 转向标记

“从现有修复基线做有限的依赖解除”说的是代码与消费合同：普通 frozen 不再被迫经过旧 W3 的 native authority、retained-source-only loader 和完整启动来源证明。它不是要求拆散、删除或模糊 Git 上的 reassessment 转向节点，也不是要求先回退到 spike 再重新抽取成果。

本次保留以下关系：

```text
6531b1e  已整理的 continuation 基点
├── 本 review 提交                         continuation
└── 0dfe833  单次普通 frozen 转向标记
    └── 58292d7  handoff                    reassessment
```

`0dfe833e41209b20f504f33cd137136083b2a51c` 继续是现有单次转向标记。本 review 只是评审交付，不是第二次转向，不移动 `reassessment`，不重写历史，也不启动 continuation 的强证明续作。后续如需把获批设计修订归并到既有节点，应按 owner 指定的历史组织另行操作；不能由“解除依赖”推出追加一串转向补丁、rebase、reset 或 force-push 的授权。后续实现可以按完整能力组织，但实现提交与转向标记承担不同职责。

时间线也需保持准确：Windows source 和轻量 launcher 的真实业务旅程在本次 frozen 返工前已经完成，不是这次转向新获得的成果。WA-06 是 Windows 平台向 TM 存储与检索 Core 派发的跨 Spec 修订标识，不是另一个产品版本。R4 沿用既有业务 Requirements，补齐 Task 7 前必须具备的 Core 受信输入及 fresh worker 消费；Core 9.6c/9.6d、WA-07 3.6a 和平台 7.0/7.1 的构建前成果已经形成。原合同仍将 Core 9.6b 定位为同候选 post-build 重放，而不是构建前 adapter 验收。

owner 本轮补充的实际停点是：在平台 7.2、Core 9.6b 的返工过程中叫停，要求 step back 与 reassessment。不得把构建前成果写成完整 frozen 已完成，也不应把这次讨论再次扩张成 source 迁移或 R4 是否值得复用的论证。Git 历史经过整理，其提交顺序不能代替实际决策时间线。

## 2. 三种身份属于什么层级：主要是 Design，不是新增 Steering 制度

认同区分候选、检索兼容和单次运行身份，但这里首先是三个不同问题，不是预先决定增加三个 authority、三个公共 schema 或三个基础框架。

| 概念 | 回答的问题 | 建议归属与边界 |
| --- | --- | --- |
| 候选身份 | 当前运行、parent/worker 配对及本次验收对应哪份发行物？ | Windows platform 的 build/entry/transport Design 定义来源与验证；Core 消费必要的关联事实，不由平台签发检索资格。 |
| 检索兼容身份 | 本机已经取得的 Gate D 资格能否用于当前检索实现？ | TM Core 的 qualification/compatibility Design，遵守 ADR-013；平台提供实际运行时及相关执行产物身份，Core 决定资格作用域与重授权。 |
| 单次运行身份与 generation | 结果是否属于当前仍可提交的执行？取消、重试或新 generation 后能否拒绝旧结果？ | Core 保持 request/result、receipt 与 publication 的权威；Feature5/Host 负责调度、撤销与通知消费；平台负责其创建的 child/IPC 生命周期。优先复用现有 session、epoch、generation、请求关联，不重复造一套运行身份系统。 |

Steering/ADR 保留长期边界：普通桌面信任范围、数据保护、Core 是能力发布 owner、设备本地资格、不同证据用途不得混淆。Requirements 描述用户可观察结果，例如错误候选结果不能被发布、兼容资格能够恢复、失配只关闭 Fuzzy。Design 才定义身份输入、字段、接口、比较点、缓存与撤销。Tasks 与验收负责证明这些设计真正被消费者使用。

建议在现有 Windows Design 增加一个短小的“普通 frozen 身份与消费边界”小节，并在 Core、Feature5 的 owning Design 中修订实际受影响合同，互相引用。不新立一个身份治理 Spec，不为三个名词各写一份 Steering，不为了“正式化”新增一串 ADR。若接口细节确实过长，可放在 owning Spec 的单一合同附页；它仍是设计说明，不形成第二审批或业务权威。

ADR-028 第 6 条“绑定实际执行产物”的具体解释如有歧义，应精确指出并由 owner 确认；不能默默降格成一个自报 digest，也不能反向扩张成完整 code-object 等价证明。必要的长期语义调整沿既有 ADR 规则处理，字段和局部接缝通常留在 Design。

## 3. 认同的方向与需要收窄的机制

**认同普通 PyInstaller onedir/windowed、same-EXE fresh worker、复用 Host 发布生命周期，以及区分候选身份与持久检索资格身份。** 这是一条在已有业务基线上闭合 0.5.2 的路线，不是放弃 Gate 或用烟测替代业务。

### 受控构建与实际执行绑定

固定并记录构建工具、依赖、owner 输入、收集配方、实际产物清单和摘要；真实业务和 Gate 必须在该候选上运行。构建工具链和发行物的信任边界按 ADR-028 声明，不声称抵御已经控制同一用户安装文件或进程的攻击者。

不建议把“逐项重新 compile 源码并与 PYZ code object 字段比较”设成普通发行默认硬门。这样的比较可以是针对具体构建风险的诊断工具，但不能仅因已有小探针就升级成新的完整证明工程。只有在指出受控构建记录、包内输入核对和真实候选验收无法覆盖的具体风险后，才讨论增加有界检查。

构建清单仍须对应实际产物，而非只记录计划输入；任意 PASS JSON、`sys.frozen` 或旁置源码都不能代替真实 Core 执行。减少运行时源码证明不等于取消输入追溯和消费验证。

### `proof-inputs` 的用途边界

Gate 实际需要的 contract、fixture 与 owner 声明属于正常运行输入，应随包提供。仅为兼容源码 fingerprint 接口而携带的源码副本是另一回事。

优先让 packaged 消费接口使用受控构建建立的实现身份和真实 Gate 输入，减少对外置 `.py` 路径、全包扫描和 AST 图重建的依赖。若一个有限的非 import 源码快照确实比当前改接口更小，可作为兼容数据保留，但必须写清确切消费者、用途及移除条件：不能作为执行 authority、不能形成另一份业务实现、不能把它的存在或摘要当作 Gate PASS，也不能通过包装旧 authority 继续承担 native reproof。

不以“彻底消灭所有源码副本”为由重写 Host；也不因接口暂时需要它就把 `proof-inputs` 固化为新的永久信任体系。

### packaged profile 与 Host

普通 packaged profile 只是明确的部署输入来源、执行上下文和错误语义，不是新的 capability issuer。不能伪装成 source/native authority，也不能把测试 adapter 提升为生产来源。

在现有组合接缝中增加最少的生产接入，复用真实 Gate runner、generation、通知、资格恢复与发布 owner。必须沿生产链贯通 session，处理 handoff 指出的 oracle/Gate D 漏传 session、结果一致性比较回读 ambient source fingerprint 等问题。纯结果一致性比较不应让结果 DTO 持有活 authority；当前运行时身份核验由持有真实 session 的 owner 完成。

普通路径不应再无条件构造完整 source anchor 图，或经新名字回到旧 native producer。保留旧路线代码可供研究与回归参考，不代表它必须进入普通候选。禁止复制整份 Host、维护两套业务发布权威或先建设一个通用 profile 框架才接第一个真实消费者。

### 资格 key、缓存与单次执行

完整候选身份用于本次运行、parent/worker 一致性与发行验收，不直接作为持久 Gate D compatibility key。资格 key 仍由 Core 按 ADR-013 绑定设备、检索实现、proof/contract、Gate C、相关 Python/SQLite/Unicode/platform 和 intended path 等影响因素。

不能因 UI、头像或其他无关资源变化就自动要求 100k 重验；也不能为稳定 key 漏掉真正影响检索、性能或测量的 worker/打包/运行时变化。资格依赖集合应由 owner 声明和验证，不靠任意手工版本号保证。普通 packaged 与 source/W3 的证据不得因字段相似而未经设计互认。

按候选缓存稳定输入事实可以减少重复工作，但缓存不能自签 PASS，不能越过 session 撤销或 generation 变化。正常启动只恢复可验证的兼容资格，不自动运行 100k；失配、损坏或缺失只关闭 Fuzzy，Exact/Context 保持各自能力，用户显式重验后由 Core 重新发布。

### worker 与取消

保留 same-EXE、fresh process、Core 严格 request/result codec、超时、退出码和既有 RSS/测量口径；没有外部 venv 或进程内假 worker fallback。windowed 传输使用明确的二进制通道，不能假定 Python 标准流或 `.buffer` 总是存在。

取消必须同时闭合发布和进程生命周期：先撤销尚未提交结果的发布资格，再由 parent 协调其创建的 child 停止、退出确认与句柄回收；Qt 不阻塞等待 child。晚到结果不能重新开放能力。若合法原子提交已经先于取消完成，按既有提交协议记录完成，不能事后伪称零提交。父进程异常结束时 child 的存活与回收策略也需在实际 transport 中验证，但不借此扩张成通用进程监管框架。

性能诊断可分开记录启动、输入处理、migration、oracle、query、publication 与退出成本；正式 Gate 仍按原 contract 计量，不通过移走成本或改变采样制造 PASS。小样本只用于定位集成问题，不签发正式性能资格。

## 4. 先完成哪些规格准备：R10–R12 与对应任务

这里确认的是修订工作范围，不是声称本 review 已经完成正式规格修改。修订应落在普通 frozen 的 owning Specs；本次不把 continuation 的 W3 正文直接改成普通路线，也不重开已完成的 source Requirements 1–9。

| 位置 | 必须收束的内容 | 完成定义 |
| --- | --- | --- |
| Windows Requirement 10 与相关 Design/Tasks | 从 retained-source/native-entry 强制执行形式，改为普通 packaged 的实际输入、执行关联、真实 Core 消费和失败行为；明确 source/W3/packaged 的适用范围。 | 活动普通任务不再隐含要求旧 W3 闭包；来源与 owner 消费完整对应，没有冻结布尔或测试 adapter 旁路。 |
| Requirement 11 | 替换不再适用的 source-only 资源要求；明确普通 onedir/windowed 内容、入口、资源定位、候选清单和用户数据位置。 | 无 checkout/开发机 venv/CWD 依赖，资源通过真实入口消费；默认播种不覆盖用户已有配置与数据。 |
| Requirement 12 与各 owner 验收条款 | 明确候选必测、未改变 owner 证据可复用部分，以及由具体变更触发的重验；保留必要数据保护和正式 C/D。 | 每个阻塞项明确对应风险、观察结果、证据来源与重验触发器，不以“全量回归”四字代替范围，也不静默跳过当前 mandatory 条款。 |
| 原 Task 7–10、Core/WA-07 相邻任务及 ledger | 将已完成的构建前成果、旧路线专用实现和普通候选待做工作区分；修订实际受影响依赖，不追加无关 amendment。 | 首条普通生产调用链不再等待完整 W3；完成标记不被重解释，替代设计审批与实施状态明确。 |

只在长篇旧正文开头加“服从 ADR-028”仍容易导致执行者误读。应修改实际驱动普通路线的条款和依赖引用，同时保留必要历史索引；不是复制一套新的平行规格或重整整个项目文档。

建议采用以下证据划分，并由 owning Spec 明确纳入正式验收：

| 范围 | 应在普通候选上观察的现象 | 可复用部分与重验条件 |
| --- | --- | --- |
| 入口/资源/独立部署 | 正常首页与项目参数、真实 Qt；无 checkout/外部 venv、非仓库 CWD、干净用户；资源定位正确且不覆盖既有数据。 | source 证据只支持原行为基线，不能代替 packaged 路径。 |
| Project/TM/TMX/FTS5 | 编辑保存重开、TM 激活与重启恢复、TMX 直接导入、FTS5 建索引/查询/重开与实际建议消费；保持 TMX ResourcePackage export-only 边界。 | 未变的 Parser、排序及业务 codec 细节可引用 owner 测试；包内入口、资源和消费者仍须实际贯通。 |
| 数据保护与恢复 | 实际支持环境中的锁竞争、相关发布失败/中断恢复、权限拒绝和用户数据保全。 | 未变的底层算法/端口证据可作支持；平台 backend、ACL/MIC、发布/恢复协议、打包 runtime 或消费者边界改变时按风险重验。既有 mandatory 矩阵不可由 review 自动降级。 |
| Gate 与本机资格 | 同一最终候选的真实 Gate C/D、正式 oracle/100k intended paths；资格发布、重启恢复、失配关闭 Fuzzy、显式重验。 | 不能用旧 source PASS 放行 EXE；不改 contract/阈值。持久资格复用与发行候选验收是两件事。 |
| worker/取消/退出 | 两类真实 fresh child、严格 IPC、畸形/截断结果和超时；标准流不可用情形；取消后的无过期发布与 child 回收，正常/异常退出。 | 构建前 adapter、模拟 authority 与小机制探针只能支持局部实现，不能代替真实 transport。 |
| source/macOS/Linux 回归 | 本次共享代码变化所影响的既有能力不回退。 | 具体范围及可复用证据写入任务；无关 UI/资源变化不自动触发所有历史证明，涉及共享语义的变更也不能被误记为打包专属。 |

稳定事实尽量在构建或发行验收建立；真实数据访问、并发、资格恢复和发布时机的检查仍在对应运行时 owner 执行。当前没有新的 Windows 运行结果，本文不签发任何 PASS。

## 5. 最小推进顺序与完整能力退出条件

0.5.2 仍只有一次完整产品交付。以下是内部实施顺序，不是拆分出的多个发行承诺：

```text
普通路线活动合同与设计获批
    → 首条普通 EXE 真实业务/Core/worker 调用链
    → 正式 C/D 与本机资格、取消/恢复闭环
    → 冻结最终候选并完成同候选产品验收
```

**能力任务一：普通 EXE 的真实调用链。** 正常用户入口、资源和 user data 位置进入现有 composition，真实 Core 消费输入，实际 migration/query child 经 same-EXE 路径工作并把结果交回真正消费者。可用小样本定位问题，但不铸造正式 Gate D 资格。退出条件不是“adapter 能构造”或“窗口能打开”，而是无测试 authority、无开发环境回退的生产链贯通，相关取消与退出可观察、可回收。

**能力任务二：Core 资格与生命周期。** 运行原 contract 的 Gate C/D、oracle/100k，真实发布本机资格，验证重启恢复、失配、损坏、显式重验及取消/提交竞争。退出条件是正式门限和测量口径未变，Fuzzy 权限由 Core 决定，缓存/UI 状态不能代答，不存在过期结果重新授权或遗留 child。

**能力任务三：最终同候选产品验收。** 冻结候选身份，完成本节前述 Project、TM、TMX、FTS5、数据保护、运行环境及 Gate 的适用矩阵。前两个任务用于尽早消除风险的运行结果，若来自不同候选，不能拼接为最终 PASS；最终冻结候选必须满足正式活动验收。退出条件是用户无需另装 Python/Qt 即可使用已声明的完整能力，而不是先交付一个初步打包壳。

可复用的底层证据与必须重跑的候选集成检查应分别记录。发现必要局部缺陷直接在相应任务修复，不因此默认大规模重构 Host、重写 Git 历史或增加一套治理材料。

## 6. 进度检查与范围约束

每轮进度只以三项为主：**新增贯通了哪条真实能力、当前实际阻塞是什么、下一次实验要消除哪个不确定性。** 证据名称或运行记录可以附在相应能力后；测试数量、接口数量、amendment 和材料整理量不是主进度。

任务开始前写清本次生产入口、真实消费者、预期观察和失败条件。任务完成时报告实际贯通链路与剩余限制，不用“局部证明越来越完整”推断完整 EXE 已接近完成。重要工作用可评审提交保护，但工作区清理和历史整理不能再代替产品推进。

新增运行时证明前必须指出：它保护哪项已声明的产品行为或用户数据，针对什么具体风险，为什么不能在受控构建或发行验收完成，以及是否触及现有 owner 或长期边界。仅因旧 W3 有相同检查、已经投入实现，或需要证明第三方全部内部行为，均不足以自动增加首发门。

只有出现真实边界变化才重新讨论路线，例如普通打包确实无法承载既有消费者、实际关键依赖被排除在资格 key 外、修复必须改变 Gate contract/威胁模型/业务 ownership，或出现当前声明内的数据损坏与不可闭合生命周期。普通接口漏传、打包遗漏和局部退出缺陷先按现有任务修复，不把每个开放问题升级为 ADR。

本次目标是让进度与完成定义规范化，而不是降低安全、性能或审查强度：停止扩大超出普通发行承诺的证明系统，继续闭合数据保护、真实检索、进程生命周期与同候选用户旅程。

## 7. 依据与证据边界

主要依据为以下固定基线文件及 owner 本轮对停点、review 目标分支和进度定义的补充：

- [reassessment handoff（58292d7）](https://github.com/BeFringe/LocalCAT/blob/58292d7c667520dbc4782e29ba0579a23957fa7f/HANDOFF-WINDOWS-FROZEN.md)：路线、证据限度、实际消费缺口与待评审方案。
- [ADR-028](.kiro/steering/adr/adr-028.md)、[ADR-013](.kiro/steering/adr/adr-013.md)：普通发行范围与设备本地资格。
- [Task 7 前置消费合同](.kiro/specs/windows-platform-enablement/task7-prebuild-consumption-amendment.md)、[Core 输入与发布合同](.kiro/specs/tm-storage-retrieval-index/trusted-input-publication.md)：R4、9.6c/9.6d、WA-07 3.6a、9.6b 的职责与既有发布语义。
- [AGENTS.md](AGENTS.md)：Steering 与 feature Specs 的分工。本 review 是路线评审，不把任务实现验收模板中的 APPROVED 当作替代设计批准。

远端比较确认 `6531b1e → 58292d7` 为两个提交，差异仅在文档；普通 frozen 仍不是已完成的代码路径。本轮未访问本地归档原始日志，未运行 Windows/Qt/native 或 100k benchmark。接收本 review 不改变这些事实。
