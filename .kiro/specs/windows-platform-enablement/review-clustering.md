# Windows Platform Enablement 审计分簇协议

## 目的与归属

本协议把 `windows-platform-enablement/tasks.md` 的 Task 0～10组织为累计实施/审计簇，使 reviewer 在同一 authority、故障模型或发行旅程完整闭合后读取 `cluster-base..cluster-tip`。它沿用 Feature 5 开始采用的 cumulative review 方法，但不复制 Feature 5 Core 的 A～O 业务簇，也不改变本 Spec 的 Requirements、Design、Tasks、checkbox、依赖或 owner。

本文件归 `windows-platform-enablement` Spec 内部，而不放入 `.kiro/steering/`：簇地图随本 Spec task graph、Windows反例和base/tip演化，不是项目级长期产品/架构事实。Steering 只在 governance 分支记录 Windows owning scope、roadmap、正式 ADR 与必要链接；若未来要把累计审计抽象成全项目规则，另由 Governance owner提炼通用规则，本文件仍保留Windows具体地图。

## 执行角色与证据边界

- **Parent / Spec owner**：加载获批R/D/T/ADR，发出自包含assignment，核对用户WIP与diff，执行目标业务API验收，显式暂存并提交；不把subagent结论直接当完成事实。
- **Implementer**：承担合同、Win32 FFI、并发/恢复、bootstrap/packaging和consumer集成实现；只修改assignment授权边界，交付task-focused evidence。
- **Cumulative reviewer**：使用独立于实现的推理上下文，对累计diff、共享不变量、反例和目标业务API证据做对抗性审计；finding关闭后复核固定tip。
- Parent可按需派发只读scout处理POSIX primitive inventory、日志场景、artifact/checksum列表、命令/环境矩阵和缺项扫描；scout不进入effort矩阵，其输出不能批准authority、durability、recovery、source trust或Feature GO，也不能替代cumulative review。
- Agent/model、branch 与 worktree 都是执行或血缘载体而非 ownership；Spec/合同才是 owner。单个执行者或独立 reviewer 可以覆盖多个 Spec，派发记录实际读写范围、base/tip和返回证据；外部provider仍需用户对data boundary的明确授权。

## 每个子任务与审计簇的完成门

每个可执行子任务仍依次完成：

1. 写出验证锚点：`Task N 完成后应看到的目标业务现象`以及依赖的本步能力；健康检查不能替代下一步使用的第一个业务API。
2. implementer读取owning Spec、前置ADR/amendment和当前tip，完成精确任务边界及task-focused validation。
3. Parent用fresh evidence验证完成条件、隐性依赖、用户WIP和五类治理门，确认本任务没有把必要不变量推迟给后续任务。
4. 簇内保持task-focused diff/evidence边界，但不要求逐task小步提交；不使用`git add .`/`git add -A`，不吸收相邻Spec或用户WIP。
5. cumulative reviewer读取固定的cluster working diff/tip、共享故障矩阵和task reports；全部finding关闭并通过fresh cluster suite后，Parent才以显式路径形成一次cluster提交。需在owning lineage branch独立形成的WA amendment commit仍按ledger执行，不被本规则压成Windows平台提交。

cluster review不能替代task-focused validation，多个局部scout也不能替代累计diff审查。任一Critical/Important finding未关闭、required test被skip、目标业务API未运行或evidence绑定旧source时，对应簇保持未完成。

## 审计簇地图

| Cluster | Tasks | 共享心智模型 / 验证目标 | impl | review |
|---|---|---|---|---|
| **C0 — Governance, ownership and dispatch** | 0.1～0.6 | ADR-020～025正式promotion/取代关系、Windows ownership、WA/WR派发、merge ledger schema、独立设计反例与NO-GO/GO边界 | `medium` | `high` |
| **C1S — Baseline and route evidence** | 1.1～1.4 | b925b80现场矩阵、portable evidence schema、Windows API inventory与stock native-entry NO-GO；只关闭source主线的调查前置，不宣称W3可行 | `high` | `xhigh` |
| **C1F — W3 custom entry feasibility** | 1.5～1.6 | custom in-process entry/toolchain/ABI/TCB reapproval与最小TrustedSourceLoader spike；1.6按`2.1→2.2→2.3→2.4→3.1→3.2`进入 | `xhigh` | `xhigh` |
| **C2 — Shared contracts and POSIX parity** | 2.1～2.4 | opaque authority/identity/lock/publish/private contracts、POSIX characterization/extraction、composition fail-closed | `high` | `high` |
| **C3A — Windows native rooted authority** | 3.1～3.2 | Win32 FFI/RAII/host Gate与逐组件rooted handle authority；形成C1F所需的稳定实现锚 | `xhigh` | `xhigh` |
| **C3B — Windows lock, private and documented publish** | 3.3～3.7 | LockFileEx首次初始化与crash接管、provider-agnostic process-primary SID/token与ACL/MIC private proof、FileId reuse、`WindowsDocumentedPublishV1`的rename→close→reopen/readback→owner commit→terminal reproof，以及instruction fault/process termination/应用重启/正常OS reboot反例闭包；不建立硬件实验室门 | `xhigh` | `xhigh` |
| **C4S — Startup and Source/Writer** | 4.1～4.4 | WA-02移除`fcntl`启动阻塞、WA-01 Parser rooted Source/Writer、Chunk recovery、真实Qt source startup | `high` | `xhigh` |
| **C5S — Project, Resource and TMX persistence** | 5.1～5.5 | WA-03/04/05 source阶段、owner lease、deterministic carrier、canonical save/import、old/new/recovery-only | `xhigh` | `xhigh` |
| **C6S — TM authority and source UI composition** | 6.1～6.6b | WA-06/07 source阶段、initial activation、唯一generation、W2 re-attestation、FTS5 reopen、launcher实现→WA-08 journey→source milestone汇合 | `xhigh` | `xhigh` |
| **C7F — Frozen distribution and Qt journey** | 7.0～7.5 | pre-build consumer、source/fixture closure、bootstrap trust handoff、onedir/windowed候选、post-build owner revalidation与WA-08 frozen journey | `xhigh` | `xhigh` |
| **C8 — Packaged Windows business E2E** | 8.1～8.5 | real EXE Qt/Project/TM/TMX/FTS5、concurrency/recovery和frozen Gate，禁止source-only代答 | `xhigh` | `xhigh` |
| **C9 — Cross-platform and evidence convergence** | 9.1～9.4 | direct primitive/closure静态门、macOS/Linux parity、完整日志/checklists与最终治理diff审查 | `high` | `xhigh` |
| **C10 — Final Windows Feature GO** | 10.1 | 同一clean user/commit/dist完成启动、项目重开、TM重启、TMX、FTS5、锁/恢复、frozen visibility并形成唯一发布裁决 | `xhigh` | `xhigh` |

`medium`/`high`/`xhigh` 是按任务语义风险给出的审慎级别，不绑定model/provider或固定派发方式；review effort不得低于implementation effort。

## Critical Path 与隐性依赖

```text
C0 Governance
  -> C1S baseline + stock W3 NO-GO
  -> C2 shared contracts/POSIX parity
  -> C3A Windows native rooted authority
  -> [C1F custom minimal spike || C3B Windows lock/private/durable publish]
  -> C3B complete
  -> C4S [WA-02 startup + WA-01 Source/Writer]
  -> [C5S Project/Resource/TMX || C6S TM prerequisites where dependencies permit]
  -> C6S TM authority + Feature5/UI source + launcher -> WA-08S -> source milestone

C1S -> C1F plan
C2 (2.1 -> 2.4) -> C3A (3.1 -> 3.2) -> C1F custom minimal spike
C1F PASS + C6S -> C7F pre-build consumer -> candidate build -> owner frozen revalidation -> Qt journey
  -> C8 packaged business E2E
  -> C9 cross-platform/evidence convergence
  -> C10 final Feature GO
```

关键隐性依赖：

- C4S的第一个可观察目标“Qt source window可以启动”直接依赖WA-02删除Chunk顶层`fcntl`；WA-01不代答该import阻塞，但后续Project/TMX/source workflow必须实际使用WA-01 rooted Source/Writer。
- C3B的publish成功必须实际闭合`WindowsDocumentedPublishV1`，不能由单个process fault test代答；C5/C6还依赖instruction fault、process termination、应用重启与正常OS reboot的old/new/recovery-only矩阵。该证据不得命名或解释为forced-power-loss硬件认证。
- C3B的private正向/负向矩阵按process-primary token/SID shape阻断：standard/elevated、同SID low/restricted、thread impersonation及service/AppContainer/impersonation边界必须覆盖；domain/Entra只可optional非阻断，不得因环境缺失形成mandatory skip。
- C3B的LockFileEx正常crash release不代答首次载体初始化；必须独立覆盖two-creator与create/write/flush/readback/close各边界creator crash接管。
- C1S的stock NO-GO与外置`.py`存在都不代答W3 feasibility；C1F必须以custom in-process entry实际通过retained-handle/executed-byte、loader attestation、origin/co_filename与fixture handle-read验证。
- Task 1.6严格按`2.1→2.2→2.3→2.4→3.1→3.2`进入；C3A在3.2形成已审查的稳定提交锚后，C1F可与C3B并行，3.3～3.7不是W3 spike的技术前置。
- C4S～C6S只接受rooted source authority并完成真实业务API；不得因没有W3而跳过source consumer，也不得把`WINDOWS_USER_MANAGED_RUNTIME_VERIFIED`提升为packaged release。
- C6S的SQLite compile option不代答FTS5；C8/C10必须用真实published store执行create/MATCH/close/reopen业务查询。
- C8的source runtime PASS不代答EXE；C10只接受同一dist、non-repository CWD、clean user的完整业务旅程。

## Cluster Base、Tip 与进入条件

- `cluster-base`是该簇首个in-scope实现提交的父提交；`cluster-tip`是最后一个task/remediation提交。实际full hash写入该簇evidence manifest或owning task的真实信息增量，不反复改写本协议。
- C0以本次独立docs baseline commit为输入；它只有在正式ADR、Steering scope、WA/WR owner acknowledgement、ledger schema与cumulative adversarial review全部闭合后退出。
- WA amendment必须由owning Spec获批，并在其lineage branch形成可追踪commit后再merge到Windows线；reviewer不得读取复制来的patch-equivalent工作树并把它视为正式merge。
- 进入一个簇前，其所属source或frozen路线的显式依赖和上述隐性依赖都必须由fresh evidence满足；C1F/W3不再是C2、C3A/C3B或C4S～C6S的前序authority gate，但仍是任何frozen consumer、C7F及后续发行簇的硬门。
- 任何实施期新事实跨越canonical authority、persistent schema、publish/recovery、dependency/composition或cross-Spec frozen ownership门时，停止该簇并回到Design/ADR；不以审计finding或Implementation Note补授权。

## 累计审计输入与退出证据

每次cluster review assignment至少包含：

- full base/tip、commit graph与累计diff；
- owning Requirements/Design/Tasks、正式ADR mapping、WA/WR ledger状态；
- task-focused reports、exact命令/版本/exit code、stdout/stderr与artifact SHA-256；
- 本簇共享正向、竞争、tamper、instruction fault/process termination/应用重启/正常OS reboot、recovery和body-safe failure矩阵；C3B另须证明arm前`DURABILITY_UNAVAILABLE`零命名mutation、arm后`RECOVERY_REQUIRED`，并记录provider-agnostic token mandatory/optional分类；
- 用户WIP/相邻只读Spec未被吸收的status与hash保护事实；
- 对下一个簇首个目标业务API的实际可用性证明。

退出时记录`APPROVED`或`REJECTED`、未决findings、fresh cluster suite、base/tip和portable evidence key。只有C10在当前tip全部mandatory gates绿色时可把Windows状态从`NOT_VERIFIED`改为`VERIFIED`；任一早期簇通过都只是局部能力事实。
