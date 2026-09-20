# Windows frozen 路线复核交接

日期：2026-09-20。用途：供项目 owner 邀请的外部评审者独立分析技术路线、实施节奏与下一步范围。

本文只属于临时评审分支，不是新的 ADR、正式 Spec、实施授权或验收报告。项目 owner 已批准 ADR-028 的产品边界，但对最近提出的普通 frozen 消费设计明确要求“先调整设计，暂不实施”。请挑战本文的推断和方案，不必替现有方案背书，也请不要未经确认直接继续实现。

## 1. 需要回答的核心问题

LocalCAT 的 Windows source 产品闭环已完成。现在要让用户无需安装 Python、Qt 等开发环境，交付普通 PyInstaller onedir/windowed 应用。此前为完整启动来源证明设计的 W3，在最小 spike 通过后仍持续扩张到 Core 输入、worker、Qt 生命周期、第三方运行时和性能证明，尚未交付完整 frozen 产品。

请评估：从 continuation 的强证明实现转向 reassessment 的普通 frozen 是否合理；当前保留的基线、ADR-028 的取代范围和拟议消费设计，是否真正减小了不必要的复杂度，还是把同一套强证明换名搬进了新路线。目标是精简、明确且能完成的产品设计，不是用烟测替代业务验收，也不是把所有潜在攻击都变成首发门槛。

## 2. 产品目标与已经确定的范围

- **source 已完成**：Windows 11、CPython 3.14 x64、专用 venv、source、轻量 Windows launcher；用户安装运行时并保留源码。W3 不再作为 source 平台迁移或交付的前置门。
- **0.5.2 是 Windows 首次 frozen 的一个完整交付**：优先普通 PyInstaller onedir/windowed。构建、Qt/资源/SQLite 烟测、真实交互是同一目标的实施步骤；最终在同一候选上完成 Project、TM、TMX、FTS5 用户旅程、必要的数据保护和 Core Gate C/D。不能拆成“初步可行性已交付”和“以后再补业务”。
- **后续产品路线**：0.5.3 规划 `rpy-project-codec` 与 `cross-device-sync-plugin`；macOS frozen 尚未立项，可能为 0.5.4，不能据此扩张本次任务。
- **本次未要求**：onefile、安装器、代码签名、自动更新、完整同用户对抗安全产品或第三方运行时执行监控。
- **产品演进需保留**：Trie/JSONL、Excel 工作流、Qt 编辑器、SQLite canonical TM、Parser/多文档与交换能力，最后才是 Windows 交付。整理交付文档不能抹掉前面的 feature 信息。

当前使用说明和里程碑见 [README](README.md)，边界清单见 [delivery-boundaries](.kiro/steering/delivery-boundaries.md)。

## 3. 两条路线的 Git 关系

这是共享已修复基线后的两条 Git 路线，不是要求删除重做 Core，也不是两个相互依赖的发行阶段。

```text
94aa29f  ADR-027 之后的共同基线
   │
4a37c82  治理提交：ADR-028 与相应 Steering
   │
   …     Windows 兼容改造、source、W3 spike 等既有开发提交
   │
ae472b0  spike 后的整理基点
   │
4b43e26  frozen 构建前消费合同
   │
f5d559b  Core 受信输入、独立 worker 与原子发布
   │
cf8ce14  Host/Qt 受信输入与生命周期
   │
8e054db  owner 驱动的构建清单
   │
6f0f4c0  producer 构建输入与固定启动入口
   │
6531b1e  source 交付与强证明边界说明 ← continuation
   │
0dfe833  普通 frozen 转向与独立文档清理 ← reassessment
   │
本文交接提交 ← 临时评审分支
```

其中六个 post-spike 提交是依次相接的线性历史，按完整能力整合了返工。历史已按 owner 授权重写，不再保留“重开—修复—归档”作为正式提交链，也不保留原 continuation 的膨胀过程提交。治理提交在共享历史中，治理分支不承担某条产品路线的专属状态；历史重排后，Git 顺序不能被当作每项决策实际发生时间。

关键锚点：

| 用途 | 完整提交 |
| --- | --- |
| spike 后基点 | `ae472b0be99fe00046d8930fa7f4d90c3364d578` |
| continuation 整理后 tip | `6531b1e334ed99993aa74f887f3a1c24f775345e` |
| reassessment 转向节点 | `0dfe833e41209b20f504f33cd137136083b2a51c` |

本次临时本地分支为 `temp/reassessment-handoff`，推送目标为 `origin/reassessment`。continuation 是其祖先，因此外援可以直接按上述 SHA 查看，不需要另有远程 continuation 分支。正式本地两条路线不会因本文新增提交而移动。

`6531b1e → 0dfe833` 只修改文档：撤下六份旧 W3 阶段报告/材料目录并明确旧条款的适用范围；代码、测试、packaging 和 tools 相同。它保留了转向节点，但**尚未实现普通 frozen**。代码相同也意味着 reassessment 仍带着需要处理的 W3 耦合。

## 4. 可以依靠的成果与证据限度

| 层级 | 已取得的结果 | 不能由此推出 |
| --- | --- | --- |
| Windows source | launcher 和真实 Qt 用户旅程已有提交证据；项目保存/重开、TM 激活/重启恢复、TMX、FTS5、资源等已验收 | EXE 的同一旅程已通过 |
| W3 最小 spike | 原强证明启动路径形成了可工作的最小实验，相关合同和实现保留 | 完整 Python/Qt 闭包、所有 owner 或性能门已完成 |
| R4 构建前消费 | Core 9.6c/9.6d、Feature5 3.6a、平台 7.0/7.1 的实现、测试及消费合同已整合 | Task 7.2 生产集成或普通 frozen 已完成 |
| 干净工作树整合核验 | 本地整理记录为 Core 223、Host/Qt 41、清单 69、producer/native 18，共 351 项、无跳过；native 测试实际使用固定 MSVC x64 编译 | 当前交接提交新跑了全套测试，或存在完整产品 frozen PASS |
| 普通 PyInstaller 机制探针 | 小程序验证过 windowed 同 EXE 的显式 Win32 二进制管道，以及实际 PYZ 代码与编译输入的比较、修改常量负控 | LocalCAT 已打包、Qt 可用、生产 worker/Gate 可用 |

source 可直接查看 [launcher 证据](windows_user_managed_launcher_evidence.json)和[业务旅程证据](qt_editor_windows_source_evidence.json)。W3 阶段报告保存在 continuation 祖先中，见末尾读取命令。不同阶段的测试数量和跳过项不能拼成一份新的验收：例如旧 Task 7.0 报告明确有一项 Core retained-handle 注入跳过，Task 7.1 的静态检查也不是零诊断；后来的 351 项是限定整合检查集合。

351 项原始日志、旧 WIP、崩溃材料和机制探针留在本地被忽略的 `artifacts/windows/`，此次不随分支上传。上表相关数字是归档记录摘要，远程 clone 单独不能核验全部原始输出；如影响结论，应向 owner 索取最小所需材料，或在声明范围内重跑。最后的历史改写只修改文档，并没有借此重新签发运行验收。

**当前不存在同一完整普通 frozen 候选的产品验收，也不存在该候选的 100k Gate D PASS。** `spec.json` 的 `ready_for_implementation` 为 `false`，frozen 状态为 `ADR-028_REDESIGN_PENDING`。历史 requirements/design/tasks 的 approved 标记只保留原 source/W3 审批，不代表最近的替代设计获批。

## 5. 三类验证必须分开讨论

| 验证目的 | 应保留的业务结果 | 需要重估的实现边界 |
| --- | --- | --- |
| 保护用户数据 | rooted 安全访问、拒绝 reparse 替换、活句柄身份、锁、可靠发布/恢复；私有密钥与资格存储的 SID、ACL、MIC 检查；失败不损坏原数据 | 不应把 POSIX mode/owner 或每个 syscall 的实现形式逐字移植；普通程序资源不自动等同于私有资格文件 |
| 证明检索语义与性能 | 真实 Matcher、排序/阈值、Gate C/D、100k 门限、oracle、设备本地资格及失效/恢复；取消后不能晚到发布 | 避免把全包源码扫描、AST 图重建和启动链证明混入 scorer 性能；避免 UI/头像改动无条件触发完整 100k 重验 |
| 证明程序与启动来源 | 受控构建、固定依赖、输入清单、实际执行产物与 owner 输入绑定、必要入口和加载路径检查、同候选验收 | 普通发行不再要求定制 native entry、完整 Boot TCB、retained-source-only loader、逐次全包扫描或第三方内部代码逐事件证明 |

前两类不是 frozen 转向时可以关闭的门；第三类也不是完全不要校验。问题是选择与普通桌面信任范围相称的证明，且放在合适阶段。默认信任用户安装的发行物、固定运行时与操作系统加载基础，不承诺抵御已经控制同一用户安装文件或进程的攻击者。

文件系统实现要遵循 Windows 自己的机制：逐级目录句柄/reparse 检查、Volume ID + File ID、LockFileEx、FlushFileBuffers、write-through/无覆盖发布后重新打开验证、当前用户 SID + 实际 ACL/MIC、重启后根据 v3 journal 重新取得句柄并验证。它们维护对应不变量，不是与 POSIX openat、inode、flock、fsync、mode 的逐项机械等价；不宜再用 POSIX 的证明形式限制普通冻结产物。

业务 owner 也不随打包改变：Project/ResourcePackage 保有 manifest、preview/import/apply 语义；TMX 直接导入消费 Parser 的 rooted sealed source；TMX ResourcePackage 仍 export-only，import/apply 负向拒绝。JSONL/ResourcePackage 搬运数据，**不搬运 Fuzzy capability**；换机器后按当地运行时与实现重新证明。

## 6. ADR-022 到 ADR-028：取代了什么，尚未落实什么

以 [ADR-028](.kiro/steering/adr/adr-028.md) 的正式文字为准。它是部分取代 [ADR-022](.kiro/steering/adr/adr-022.md)，不是宣布原 W3 失败项已通过，也不是取消整包验收。

| 范围 | 当前裁决 |
| --- | --- |
| ADR-022 决策 3–8、10 及相关长期约束 | 普通 frozen 移除完整 Boot TCB、定制 native entry、retained-source-only execution 和强制 spike 前置 |
| ADR-022 决策 9/12 的派生限制 | 由上述强证明派生的构建/运行限制及完整重验触发器不再自动适用 |
| ADR-022 决策 1、2、9、11 中仍有效部分 | 保留 onedir/windowed 优先形态、ownership、构建可追溯性、真实 packaged E2E，禁止伪造 capability |
| ADR-023 决策 6 的完整 pre-authority native closure | 普通 frozen 不再强制；LOCK-first、MIC/security profile、pending publication 保留 |
| ADR-009/013 | Core 语义、性能、设备资格仍有效；不可把整个 EXE/PYZ 的每次变化直接等同于检索资格失效 |
| ADR-020/021 及后续数据端口修订 | 数据、锁、发布与私有存储保护保留；ADR-024/025/026/027 已有取代关系不撤销 |

ADR-024 曾收窄身份 provider 的强制矩阵，ADR-025 曾把正常发布保证与硬件断电实验区分开。这些经验提示需要持续审查产品声明，但不能据此任意删除保护数据的检查。

**仍有文档债务**：reassessment 在 Windows R/D/T、ledger 开头声明旧 W3 条款仅供映射，正文仍大量保留 native/source-only/完整 Boot TCB 约束。Requirement 12 和旧任务中也有跨平台整仓回归、重启及各类环境矩阵。请判断哪些仍是本次真实风险的必要验收，哪些应缩小到受影响 owner，哪些属于旧强证明路线；仅加一段“服从 ADR-028”是否足以让后续实施者不再误读。原完成勾选描述原构建前能力，不应被重解释成普通 frozen GO。

## 7. W3、WA-06 与 R4 的来龙去脉

**W3** 是原 ADR-022 的 Windows frozen 信任与启动 profile；不是 source 的第三个交付阶段。它把 self-contained 发行与自定义 native entry、完整 pre-authority 闭包、保留句柄的精确源码执行绑定。

**WA-06** 是 Windows 改造派给 `tm-storage-retrieval-index` 的 Core amendment。R1 是初始 Windows 消费要求；R2 因 ADR-023 从 DACL-only V1 切换到可表达 MIC 的私有安全 V2；R3 纳入 ADR-024 的 provider-agnostic 主体和 ADR-025 的正常发布范围，完成了 source 路径。**R4 沿用 R3 的 Requirements 语义**，补齐 frozen 构建前必须存在的两个 Core 消费实现：9.6c 受信 Gate 输入 session，9.6d 独立 migration/query worker。Feature5 3.6a 负责后台调度、通知与撤销生命周期，平台负责 producer、固定分派与构建集成。

前置缺口在 spike 完成后才被明确：证明最小启动入口能成立，并未证明真实 Core 会消费该输入，也没有证明 frozen 下独立 worker 可执行。R4 修复了这个依赖遗漏，但完成构建前端口不等于完整产品已经消费成功。原合同见 [R4 修订](.kiro/specs/windows-platform-enablement/task7-prebuild-consumption-amendment.md)、[amendment ledger](.kiro/specs/windows-platform-enablement/cross-spec-amendments.md)和[Core 输入/发布合同](.kiro/specs/tm-storage-retrieval-index/trusted-input-publication.md)。

Task 7.2 的本地 W3 集成实验曾经使用注入测试 driver，经 parent 通道启动同 EXE 的 migration/query child。归档摘要记录：样本仅 12 条，query 约 115.5 秒，不能充作 100k 性能结果，也不能直接归因于检索算法。退出访问异常引出了有序 finalization 修复；一次成功退出又早于后续 SBK/shiboken 修改，不能沿用为最后代码的验收。最后一次 vendor race 实验未到刺激点，按用户要求停止，没有 PASS。这些未收束实验不应混入已整理能力的完成声明。

历史上的真实难点包括 retained native handle、Python 解释器与线程所有权、Qt 后台回调/撤销、退出顺序和第三方库加载行为；另有重复源码/AST 证明带来的成本。请把数据竞态或资源生命周期的真实缺陷，与为了过强产品声明而增加的证明负担分开，不要简单得出“所有安全检查都是过度设计”。

## 8. 普通 frozen 当前面对的具体实施问题

以下来自当前保留代码的检查，是设计必须处理的集成点，不是已修复结果。路径是当前仓库相对路径；优先按函数名定位，避免历史改写后的行号误导。

| 集成点 | 当前情况与影响 | 希望评估的最小解法 |
| --- | --- | --- |
| [qt_editor.py](qt_editor.py) 的 `main`、`_compose_editor_controller` | 常规入口拒绝 `sys.frozen`；frozen composition 要求现有受信 authority | 增加明确的普通 packaged 入口，同时保留首页、项目参数、用户目录和错误语义 |
| [tm_gate_inputs.py](tm_gate_inputs.py) 的 `_require_native_frozen_producer` | frozen 输入要求真实内建 `_localcat_frozen_bootstrap` 的 native 身份；source adapter 拒绝 frozen | 不伪装 source/native authority，定义范围有限的普通 packaged 输入来源 |
| [capability_host.py](capability_host.py) 的 `_SourceAnchorGraph`、`_ModuleSourceCodeAnchor`、`_RuntimeFunctionIdentity` 及各 Core binding | 约五千行 Host 中，业务 owner、资格发布/生命周期与源码路径、AST/代码身份紧密耦合；已有 frozen composition 仍走源码图 | 复用真实 Gate runner、发布 owner、generation 和通知；选择必要的 composition 接缝，避免复制 Host 或维持两套业务权威 |
| [capability_frozen_inputs.py](capability_frozen_inputs.py) | W3 文件/路径 anchor 会回到 native reproof | 普通 profile 不应仅包一层旧 authority 后继续承担相同证明 |
| [tools/windows_frozen_packaging.py](tools/windows_frozen_packaging.py)、[tools/generate_windows_frozen_manifest.py](tools/generate_windows_frozen_manifest.py) | 现有构建围绕 custom runw、source-only、拒绝关键 PYZ 等约束；不是普通 PyInstaller spec | 复用 owner/资源清单的有用部分，剔除不适用限制，明确真正执行的业务代码如何与输入关联 |
| [frozen_worker_transport.py](frozen_worker_transport.py)、[frozen_worker_entry.py](frozen_worker_entry.py) | 依赖 native authority 与固定 same-EXE dispatch；parent transport 使用 `subprocess.run` | 普通 same-EXE migration/query worker、严格 IPC/超时/退出/RSS，且没有外部 venv fallback |
| [tm_benchmark_oracle.py](tm_benchmark_oracle.py)、[tm_benchmark_gate.py](tm_benchmark_gate.py) | oracle 的若干 fingerprint 调用未传当前 `_input_session`；Gate D 调 oracle 时也未贯通它 | 明确沿实际 execution context 传递 session，保持算法/oracle 不变 |
| [tm_benchmark_query_process.py](tm_benchmark_query_process.py) 的 `_adjudicate_evidence_against_process_evidence` | 比较过程仍读取 ambient source fingerprint，且由结果对象构造和 Gate 汇总路径调用 | 分离纯证据一致性比较与当前运行时身份校验；后者留在持有真实 session 的 owner，不向普通结果 DTO 注入活 authority |
| Qt 撤销与 [tm_benchmark_worker.py](tm_benchmark_worker.py) | UI 侧 Event/关闭输入会撤销发布，但不能据此证明正在运行的独立 child 已退出；现有 child runner 没有即时取消通路 | 让取消传到 parent transport，只回收自己创建的进程；UI 不阻塞 join；验证取消后无晚到发布、无残留 child |
| windowed 标准流 | 普通 `--windowed` 不能假定 Python 标准流一定可用，也不能直接依赖 `.buffer` | 显式继承的二进制管道与限定启动模式；真 worker 实测，不以 GUI 烟测代替 |
| [frozen_product_entry.py](frozen_product_entry.py) 的 `run_product` | W3 最小入口启动空编辑器，不能等同于 source 的正常首页/项目参数路径 | 统一用户入口行为；默认资源只在缺失时播种，不覆盖用户配置/数据 |

可复用的真实执行入口包括 `tm_retrieval_validation._recompute_retrieval_validation_from_session`、`tm_benchmark_gate._run_benchmark_gate_d_from_session`、`tm_benchmark._benchmark_implementation_fingerprint_from_session`。这些接口证明存在复用接缝，不证明普通 producer/consumer 已实现。

这里的两个 session 传播问题是比“R4 构建前任务已完成”更深一层的实际消费遗漏；应审查测试为何未覆盖生产调用链，而不是只为新 profile 再复制一组只证明 adapter 能被构造的测试。

## 9. 最近提出、但尚未获批的替代设计

以下是供挑战的候选思路，不是结论，也不要求外援沿此方案实施。原草案已从正式 Spec 提交中撤下，保存在本地归档；本文列出会影响评估的实质内容。

1. 使用标准 `Analysis/PYZ/EXE(console=False)/COLLECT` 与 stock bootloader。沿现有实验版本 CPython 3.14.7、PySide6 6.11.1、PyInstaller 6.22.2、openpyxl 3.1.5 固定构建输入；这是实验组合，不是宣称应升级到这些包的最新版本。
2. 在受控构建时检查实际 EXE/CArchive/PYZ 中的 owner 代码与编译输入相符，仅归一化已知 `co_filename` 路径，保留真实代码字段；不再运行自定义 source-only loader。
3. 由于 Core 的既有 fingerprint 仍读源码/contract/fixture，曾提议携带非 import 用途的 `proof-inputs/`，并在构建时与实际 PYZ 绑定。旁置未执行 `.py` 本身不能证明真实代码；需要评估这一兼容层是否值得保留，或应直接减少源码证明接口。
4. 每进程建立有限的 packaged runtime 上下文：候选身份、选定入口/运行时检查、不可变输入缓存与撤销。在已有 Core binding 增加受限构造入口，保留现有 Host 发布生命周期，避免复制整个 `capability_host.py`。
5. 分开候选身份与持久检索资格身份。完整候选标识用于 parent/worker 一致性与本次验收；持久 Gate D fingerprint 只绑定实际影响检索/性能的 Core 实现与相关运行时。不能把整个 PYZ/EXE digest 作为资格 key，否则头像/UI 改动会使 100k 资格失效。普通 profile 与 source/W3 的证据也不能误互认。
6. same-EXE 独立 worker 通过显式二进制管道传输；用可取消的 parent transport 管理进程，取消后终止/等待退出并关闭句柄，Qt 只发撤销请求。
7. 在同一候选上完成完整用户旅程与 Gate；跨 owner 的实际调用链、取消/关闭、复用资格、失配重验、干净用户/非仓库 CWD 等作为产品验收内容。

**请重点质疑第 2–4 项**：实际 PYZ 逐项代码比较、proof-inputs 和新的 profile 类型是否仍在重建过重的证明系统？在 ADR-028 的受控发行信任范围内，能否用更少的机制达到真实执行绑定、可追溯构建和不伪造 Gate 的目标？若需要修订 ADR-028 中具体措辞，请明确指出哪条会造成不成比例的工程成本，而不是默认新立 ADR 或默默绕开合同。

机制探针只测试了一个小程序。它通过 Win32 `GetStdHandle` 和 `msvcrt.open_osfhandle` 打开继承的二进制管道并回显；比较实际 PYZ 的 payload 代码与 `compile(..., dont_inherit=True, optimize=0)` 输出，修改常量的负控不相等。该次运行的 `stdin_none`、`stdout_none` 均为 false，所以不能说“标准流为 None 的情形已经实测通过”。本地实验没有覆盖 LocalCAT、Qt、生产 worker 或性能。

## 10. 实施节奏中已经暴露的问题

- **前置依赖发现太晚**：先完成最小启动 spike，再发现 Core 的真实受信输入与 fresh worker 没有实现，产生 R4。需要判断今后先验证哪条最短的生产调用链，才能尽早发现缺口。
- **把局部通过误当整体接近完成**：输入 adapter、清单生成器、native 启动和模拟/注入组件分别通过，仍可能不能运行真实 Gate 与 Qt 退出生命周期。新的任务应以可观察用户能力和真实消费者为边界。
- **证明范围持续增大**：完整第三方闭包、native 预加载顺序、保留句柄、AST 图和逐事件检查互相牵连，消耗了性能与调试预算。有些确实保护数据或发布，有些来自过大的威胁模型，需要逐项归属。
- **退出/撤销与证据发布混在一起**：撤销资格发布不代表后台 child 被停止；native/Qt/Python 的 finalization 成本不能靠更多“验证已关闭”标志解决。
- **文档治理代替产品进展**：amendment、ledger、阶段报告和修订记录不断增加，而普通用户仍没有可验收的完整 EXE。治理只应澄清 owner、边界和真实审批，不能重复形成同一事实的多套权威。
- **历史与工作区维护干扰主线**：C 盘长期 WIP、巨大提交、过程材料进 Git 再删除、scope 混用和追加修正文档造成额外返工。重要材料现已归档到 E 盘工作树的忽略目录；工作区清理与产品设计必须分开处理。本次不上传本机 artifacts，也不把清理动作包装成 frozen 能力。
- **代理的范围与编辑判断有偏差**：曾用交付边界改写抹掉 README 的 feature 演进；曾追加补丁式文档/设计提交，而 owner 要求修改原能力提交、保留原转向节点。现已修正。曾出现 Task 8 从 xhigh 降级的错误历史，该提交已从活动路线移除；这不是性能或产品合同的有效调整。

这些问题不意味着需要再做一次大规模重构或重写历史。当前优先是独立判断“保留什么、简化什么、下一步如何以有限任务完成一个产品候选”，而不是增加一轮抽象框架或材料整顿。

## 11. 希望外援给出的评估

请用当前代码和正式边界核对，明确区分事实、推断与需实验才能确认的事项：

1. **路线裁决**：保留 continuation 为强证明研究路线、reassessment 为普通发行路线是否合理？继承已修复基线是否比从 spike 重新抽取更稳妥？哪些旧实现应该继续复用，哪些只是保留在历史里？不预设必须回退或必须全盘保留。
2. **边界裁决**：按“用户数据 / 检索语义与性能 / 程序来源”列出必须保留、移到构建/发行时、可以删除的检查，并说明对应风险。检查 ADR-022/023→028 的取代是否遗漏，现有正文是否会继续制造错误前置。
3. **设计裁决**：对第 8、9 节给出更小的 producer/consumer、Host、worker 和 qualification 方案。特别评估证明输入与实际 PYZ 的关系、持久资格 key 的范围和缓存/撤销生命周期。
4. **路径裁决**：给出到 0.5.2 同候选验收的最小依赖顺序。可以有构建和机制实验步骤，但不要把它们另立为产品交付，也不要先完整实现另一套 W3 才允许第一个真实业务消费者运行。
5. **验收裁决**：哪些真实项目/TM/TMX/FTS5、数据保护与 C/D 检查必须落在候选上，哪些可复用未改变的 source/owner 证据？需要验证的现象、失败条件和证据应具体，不能只列“全量回归/安全/性能”。
6. **节奏裁决**：指出导致当前停滞的主要原因及优先级；区分必要修复与自我制造的证明负担。建议少量边界明确、可构建、可评审的完整能力任务，以及触发重新讨论的实际条件。

期望结果是一份简洁结论、必要边界清单、最小依赖图和下一步任务/退出条件。对无法从仓库确认的性能或 Windows 行为请指出缺失证据，不需要给没有实验依据的成功率，也不要求把每个开放问题上升为 ADR。

## 12. 建议阅读与核验入口

先读 [README](README.md)、[ADR-028](.kiro/steering/adr/adr-028.md)、[交付边界](.kiro/steering/delivery-boundaries.md)，再对照 [ADR-022](.kiro/steering/adr/adr-022.md)、[ADR-013](.kiro/steering/adr/adr-013.md)。随后读 Windows [requirements](.kiro/specs/windows-platform-enablement/requirements.md)、[design](.kiro/specs/windows-platform-enablement/design.md)、[tasks](.kiro/specs/windows-platform-enablement/tasks.md)、[spec 状态](.kiro/specs/windows-platform-enablement/spec.json)和第 7 节合同，最后按第 8 节定位生产代码。

以下为只读 Git 核验命令，在该分支完整 clone 内执行；无需访问本机工作区或取回原始 continuation 过程分支：

```sh
git log --oneline --reverse ae472b0..0dfe833
git diff --stat 6531b1e 0dfe833
git diff 6531b1e 0dfe833 -- '*.py' '*.c' '*.h' tests packaging tools

git show 6531b1e:.kiro/specs/tm-storage-retrieval-index/task96c-validation.md
git show 6531b1e:.kiro/specs/tm-storage-retrieval-index/task96d-validation.md
git show 6531b1e:.kiro/specs/feature5-ui-integration/task36a-validation.md
git show 6531b1e:.kiro/specs/windows-platform-enablement/task70-validation.md
git show 6531b1e:.kiro/specs/windows-platform-enablement/task71-validation.md
```

第三条 diff 应为空。历史报告是对应组件验收的读取入口，不是普通候选的验收替代。若要重跑代码测试，应从 [requirements-ui.txt](requirements-ui.txt) 和具体测试模块入手，先确认 Windows、Python 和 native toolchain 条件；不要把 source `--smoke-test` 的结果投射为 frozen PASS，也不要把模拟 authority 的测试当成实际 packaged 执行。

本交接提交仅添加本文，不更改生产代码、任务勾选、正式设计审批或两条路线的产品结论。
