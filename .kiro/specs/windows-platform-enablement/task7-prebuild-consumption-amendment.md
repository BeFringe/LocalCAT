# Task 7 前置消费合同修订

状态：**已批准，构建前消费待实施**。用户已批准 WA-06 R4 及平台/WA-07 的 Design/Tasks/ledger 增量。以下记录获批 W3 合同，实施与运行资格由对应 owning task 验收。

## 已批准事项

已批准下述窄范围 Design/Tasks 修订：由 TM Core 在构建前交付 Gate 源码、fixture、benchmark contract 的受信字节消费接口及两个 fresh worker 的 frozen 调用适配；平台负责独立 child 的受信 bootstrap 与固定 worker dispatch；Feature5 3.6a 消费这些接口。Core 9.6b 继续负责同一候选的 post-build 重放。

现有 Requirements、ADR-009/011/022/023 所规定的 authority、业务所有权和验收强度保持原义；没有提出新 ADR 或修改 Steering。该请求补齐实现分工与精确消费合同，不批准任何运行结果，也不把最小 spike、source observation 或 pre-build 单测作为 packaged PASS。

## 修订原因

最小 spike 只提供 native authority；原 Gate 按路径读取输入、benchmark 以 Python 模块启动 child，缺少进入完整 frozen 候选之前的消费实现。R4 因此补齐 Core 输入与 worker、Feature5 后台调度及平台 producer 的任务分工。native 原线程限制属于 W3 接线选择，不是普通 Windows 产品的永久前置。

## 已批准 Design 增量

### 1. Core 的受信输入消费

由 TM Core 拥有 Gate A/C approved roots grammar、fixture parsing、relative-id 集合、digest 算法、Gate D implementation fingerprint、benchmark contract 和各 Gate 判定。平台只提供由真实 source/frozen authority 建立的读取与复证能力；Feature5 组合并验证使用该能力的 runtime ports。

在 Core validation/benchmark 边界新增内部受信输入 session；请求以经 owner roots 声明的规范 bundle-relative id 标识输入，返回本次有效 proof window 内的 exact bytes。session 的生产构造只接受组合根验证的 authority，拒绝调用方提供任意 read callback、bytes 字典或自报 digest 来铸造 Gate/publication authority。session 不是新增 capability issuer。

source route 保留既有公开 Path 入口和 source 行为，由组合入口建立 source session；frozen route 只消费 `TrustedSourceAuthority` 背后的 manifest-bound retained reads。Core 的 JSON/TXT/源码摘要/contract 读取都通过对应 session，不能在取得 bytes 后重新打开 bundle path、复制 fixture 到临时目录、借用 checkout，或全局替换 `Path`。业务数据临时资产仍按既有 rooted platform owner 合同处理，不误归为 frozen 源码输入。

source inventory、fingerprint 和执行模块必须属于同一实现 epoch。Gate C/D 的开始、实际执行、结果构造与发布前 terminal reproof 继续绑定该 epoch；authority 关闭、内容/身份漂移、loader/code anchor 不符或过期均 fail closed。没有把启动时一次 digest/cache 变为长期 authority，也不重签旧 evidence。

### 2. 两个 fresh worker 的 frozen 启动

TM Core 拥有 request/result codec、fresh child 要求、独立 migration/query 测量、RSS scope、timeout、退出码和错误分类。平台提供受限 frozen launch mode；source route 继续使用现有 Python 模块入口。

每个 frozen child 都从**同一发行候选 EXE**的 W3 native entry 起步，独立完成 Boot TCB/native/source 验证、E10 handoff 与本进程的一次 take。父进程不序列化 authority、handle attestation 或 capability snapshot 作为 child 授权；child 不复用父进程的 Python authority。工作模式参数只选择任务，不能指定任意模块、源码路径、release digest 或跳过验证。

E10 后固定 trusted bootstrap 只允许两个内部模式，分别映射至 `tm_benchmark_process` 与 `tm_benchmark_query_process` 的既有 worker owner；默认模式进入产品启动。未知模式、重复/多余选择和通用 `-m` 全部拒绝。选择 worker 前不导入 Core、platform factory、Qt 或 SQLite。

windowed child 使用父进程创建、定向继承的 request/result pipes。E10 后才将这些传输端点接到 Core 严格 codec；端点与模式均不提供 authority。缺失/错误句柄、协议畸形、非零退出、超时和截断结果按 Core 既有失败语义处理。保留独立 PID/fresh process 与从启动到完成的 RSS 口径；不得回落 venv Python 或进程内 worker，不把 GUI 控制台 stream 可用性当作前提。

### 3. 后台消费与 native 生命周期

take、native read/reproof/close 仍在原 owner thread/interpreter 串行执行。平台 authority 内部安排请求调度，Feature5 后台 Gate 只取得对应有效 proof window 的 bytes/attestation，不能直接调用或取得 native 对象。Qt 调度适配由 WA-07 在 Boot TCB 完成后注入，平台 authority 不直接依赖 Qt；headless worker 在自己的初始线程运行同一 owner 合同，不引入 Core→Qt 依赖。

调度必须闭合队列等待、关闭、取消、异常和重入：close 先撤销新的 proof window，未完成请求失败并唤醒等待者；取消/终态不得遗留仍可发布结果的 session；UI 主线程不能等待一个反向等待主线程的 worker。Gate 发布前需要 fresh terminal reproof，队列中的旧 attestation 或缓存 bytes 不能绕过撤销。source route 保留现有回归要求。

## 已批准 Tasks 与 ledger 增量

下表记录获批时的任务分工；当前完成状态以各 owning tasks 和平台 ledger 为准。

| Owner / 文档 | 获批修改 | 完成证据与依赖 |
|---|---|---|
| Core Design | 纳入上文受信输入 session、source/frozen 双 route 与 fresh worker 消费合同 | Requirements 的 Gate/benchmark 强度不变；映射 ADR-009/022/023 |
| Core Tasks **9.6c**（新增，pre-build） | 实现 Gate A/C source/fixture、Gate D contract/fingerprint 的受信字节消费，保留 source 公开入口 | 正反单测、source 回归及独立 review；依赖 9.6a、平台 1.6 与本 amendment 批准，不依赖 7.4 |
| Core Tasks **9.6d**（新增，pre-build） | 实现 migration/query fresh child 的 source/frozen launch 选择及严格 IPC 消费 | 独立 PID、两种 worker、请求/结果/错误/超时/RSS 口径测试及独立 review；依赖 9.6c，不依赖 7.4；真实 frozen 运行归 9.6b |
| Core Tasks **9.6b**（原 post-build） | 增加 9.6c/9.6d 依赖并验收同一候选的两个 child、Gate 输入和 terminal reproof | 保留平台 7.4 依赖；完整 100k 双路径硬门和产品发布验证不得缩减 |
| 平台 Design / W3 计划 | 纳入 E10 后固定 worker dispatch、定向 pipe 传输和原线程 owner 调度/lifecycle；不扩展 pre-authority imports | 与 Task 7.2 的完整 handoff 统一实现和审查；按既有 W3 维护触发器重建 candidate inputs 并至少重跑 Task 1.6 完整 mandatory 矩阵，full candidate 仍走 7～10 |
| 平台 Tasks **7.0** | 将“WA-06 roots only”扩为 Core 9.6c/9.6d；新增这两项前置，仍集成 WA-07 3.6a | 只记录 `FROZEN_PREBUILD_COMMITTED`，不证明 native/full runtime |
| 平台 Tasks **7.2** | 在现有 native→CapabilityHost producer 任务内增加 fixed worker dispatch 与线程/lifecycle验收 | 实际 native 正反证据；不以 mock 或 Core pre-build 测试替代 |
| Feature5 Design / Tasks **3.6a** | 消费共同受信 session，明确后台 proof window/撤销和 Core 9.6c/9.6d 前置；其独立 reviewer 复核 owner 边界 | consumer implementation checkpoint；同候选 runtime 仍归 6.6b/7.4b/7.6b/9.2a |
| 平台 ledger | **WA-06 R3→R4**；R3 保留为 `SUPERSEDED` 历史，既有 source anchor/evidence不改签；pre-build 阶段新增 9.6c/9.6d | R4已据用户批准登记`ACKNOWLEDGED`；新revision的frozen状态仍待实现，最终`MERGED_PASS`禁止预填 |

WA-07 原有 3.6a 已授权 trusted bootstrap 消费；本提案只补明确的同合同消费设计、checkpoint 与依赖，不新增业务 authority 或 public capability schema，因此保持 WA-07 R1。若 owning review 发现新增 public contract，必须先提出该行的精确 revision 增量，不能借此草案默默吸收。

顺序为：批准 owner Design/Tasks/ledger → Core 9.6c → Core 9.6d → Feature5 3.6a/平台 7.0 consumer 集成 → 7.1 manifest → 7.2 完整 producer → 7.3/7.4 候选 → 7.4a 各 owner post-build → 其余 7～10。consumer pre-build 可以针对严格 test seam 验证组合逻辑，但该结果不能生成生产 authority、frozen Gate PASS 或发行状态。

各独立语义任务以实现、必要测试、独立 review 与对应 checkbox 一起提交；不把 9.6c 和 9.6d 按文件混在同一个大实现提交。草案本身只形成一个“前置缺口诊断与修订请求”文档提交。

## 验收补充与已有工作

批准后的必要反例至少包括：foreign/fake authority、错 relative-id、source/fixture/contract 替换、关闭后复用、worker stale proof、loader/code anchor 不一致；两个真实 frozen child 的未知模式/畸形 IPC/超时/错误结果；主线程调度中的取消、close、等待和 UI 响应。最终结果必须在同一个 full candidate 上满足原有 owner、Qt、SQLite、业务 E2E、100k benchmark、macOS/Linux parity 和 GO 门。

Task 7.1 的完整递归闭包、manifest 容量与布局，7.2 的完整 loader/handoff，以及 7.3 的 Qt/PySide6/SQLite 收集仍属现有已授权工作。当前 spike 最多 32 个 manifest entries，而 Gate D implementation fingerprint 已列 34 个成员，说明不能把 spike manifest 原样用于产品；这属于 7.1/7.2 的实现与重验输入，不是另一个业务 scope request。

本修订经独立审查和用户批准后纳入各 owning Design/Tasks 与 WA-06 R4 acknowledgement。过程诊断、暂停与复审记录保存在本地归档；批准事实由本合同及 owning Spec 保留，运行结果仍由对应任务验收。
