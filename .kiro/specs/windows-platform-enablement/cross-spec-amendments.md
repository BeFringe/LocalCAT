# Cross-Spec Amendment Dispatch and Integration Ledger

## Purpose and Authority

本文件是 `windows-platform-enablement` 的派发/集成计划，不是相邻 Spec 的 authority 替代物。依据 `.kiro/steering/spec-ownership.md`，每个 amendment 必须修改 owning Spec 的 Requirements/Design/Tasks、独立获批并形成可追踪提交；Spec/合同是 owner，Agent、branch 与 worktree 都不是。单个执行者或 reviewer 可以覆盖多个 Spec，Windows 分支只记录请求、依赖、提交可达性与最终 evidence。表中 task ID 是拟追加到现有 task 后的 amendment ID，采用 `x.ya`，不重排或冒充原 checkbox。

### Prefix and Disposition Vocabulary

- **`WA-*` — Windows Compatibility Amendment**：Windows 平台兼容性导致 owning Spec 的 public requirement、design boundary 或 implementation task 发生真实增量，必须补发并批准 R/D/T amendment。
- **`WR-*` — Windows Compatibility Revalidation**：既有 feature/Spec 不需要改变合同或追加实现，只需在 Windows consumer 集成后取得 fresh regression evidence。
- **`NO_AMENDMENT`**：该 feature 是已完成基线、由其他 amendment 覆盖或不属于本 Spec 的新增范围，因此既不修改 R/D/T，也不新增 task；只在最终矩阵重验适用行为。
- **`CONTRACT_CHANGE` / `REVALIDATION_ONLY` / `NO_AMENDMENT`** 是 disposition；`WA`/`WR` 是 ledger ID 前缀，两者不能互相替代。

### W1 / W2 / W3 Working Candidates

`W1`～`W3` 原为本 Spec 的 Windows ADR candidate working labels，现已分别唯一映射到正式采纳的 ADR-020/W1、ADR-021/W2、ADR-022/W3。已采纳ADR-023补充W1/W2/W3若干pre-authority顺序并窄范围修订W2 security profile；ADR-024将W2主体资格收敛为provider-agnostic current-primary-token事实，ADR-025以文档化发布合同取代W1运行时硬件耐久profile，ADR-026收窄ADR-020对Parser发布状态的无条件外推。后续依赖图和派发可继续使用短标签，但标签不构成 ADR 之外的第二份 authority，也不单独授权平台实现。

| Working label | Full candidate name | One-sentence contract | Dependency / References |
|---|---|---|---|
| **W1** | **Cross-platform Rooted File Authority, Lock and Durable Publish Boundary** | 建立共享平台文件合同，并固定 Windows live-handle rooted identity、reparse/share profiles、W1 protocol-control exact ACL与可恢复首次初始化、两种 publish mode，以及`WindowsDocumentedPublishV1`的write-through/flush、handle-bound naming、retained readback→owner commit→terminal reproof与稳定错误边界；Parser按ADR-026只保留单操作内证明，不持有journal/LKG。 | 基础候选；约束所有 WA。参考 [research.md § Governance Candidates](research.md#governance-candidates)、[design.md § Windows Native Adapter](design.md#windows-native-adapter)、[ADR-008](../../steering/adr/adr-008.md)、[ADR-009](../../steering/adr/adr-009.md)、[ADR-018](../../steering/adr/adr-018.md)、[ADR-019](../../steering/adr/adr-019.md)、[ADR-025](../../steering/adr/adr-025.md)、[ADR-026](../../steering/adr/adr-026.md) 与 [governance.md](../../settings/rules/governance.md)。 |
| **W2** | **Windows Device-local Private Attestation Representation** | ADR-021最初以`WindowsPrivateAclV1`冻结SID/DACL/AccessCheck；ADR-023以包含同一exact owner+DACL和medium MIC projection的`WindowsPrivateSecurityV2`接管V1，ADR-024进一步固定provider-agnostic current-primary-token资格与正/负token矩阵。嵌套`WindowsPrivateProof`绑定V2 profile/descriptor digest，Gate D/canonical owner envelope保持正交。 | 依赖 W1 的 identity/error contracts；仅 WA-06/07 直接依赖。参考 [design.md § Private Storage Proof](design.md#private-storage-proof)、[ADR-013](../../steering/adr/adr-013.md)、[ADR-016](../../steering/adr/adr-016.md)、[ADR-023](../../steering/adr/adr-023.md)与[ADR-024](../../steering/adr/adr-024.md)。 |
| **W3** | **Windows Frozen Distribution Ownership and Source Closure** | 建立 onedir/windowed packaging owner、native-entry pre-Python DLL policy、完整 Boot TCB、retained-handle exact-byte `TrustedSourceLoader`、真实 `.py`/fixture closure、clean/content-addressed build provenance 与 packaged release gate。 | Governance review可与W1并行；bootstrap实现依赖已采纳W1 rooted语义，WA-07/08与最终打包依赖W3。参考 [design.md § Frozen Distribution Design](design.md#frozen-distribution-design)、[ADR-009](../../steering/adr/adr-009.md)、[ADR-011](../../steering/adr/adr-011.md)及[research.md § Frozen-source](research.md#frozen-source-与-pyinstaller)。 |

依赖主线是 `W1 → Windows platform contracts/backends → WA consumers`；`W2` 在 TM private attestation 前闭合；`W3` 先做最小 frozen spike，再在 WA-07/08 集成后闭合完整发行物。未来 ADR 若取代或拆分现有决策，本 ledger 必须记录 superseding mapping，不能把 W1/W2/W3 短标签当成独立批准。

### Source / Frozen Delivery Staging

本节是一次独立、人工批准的cross-Spec delivery-staging amendment：它只把WA-01～08已批准的Windows source与frozen验收拆成累计阶段，不删除功能、不改变owner、W1/W2/W3 authority、public capability或ADR-022最终发行门。因为没有改写既有WA合同delta，它不替换`dispatch_request_revision`；但它对实际触及的owning Requirements/Design/Tasks另有下方逐owner acknowledgement，不以Windows ledger的一句说明冒充owner批准。source实现通常形成一个可追踪commit/evidence并登记`SOURCE_MERGED_PASS`；只有经owning Spec明确批准的依赖分期可以形成有序多个source commits，前置片段只登记`SOURCE_COMMITTED`，全部source suffix与真实consumer证据闭合后才进入`SOURCE_MERGED_PASS`。同一WA row只有在post-build frozen revalidation与最终consumer journey闭合后才能进入terminal `MERGED_PASS`。

- **Source lane**：`C1S → C2 → C3A → C3B → C4S → C5S/C6S → platform 6.6a launcher → WA-08 5.4a → platform 6.6b`。它使用用户安装的CPython 3.14 x64、专用venv、`requirements-ui.txt`与受审source tree；轻量Windows GUI入口只启动外部`pythonw.exe`/source bootstrap，不是发行profile且不铸造`TrustedSourceAuthority`。
- **Frozen lane**：W3 custom in-process entry计划可立即进行；gate-quality spike严格按`platform 2.1→2.2→2.3→2.4→3.1→3.2`进入。3.2所属C3A形成已审查的稳定提交锚后，C1F可与C3B（3.3～3.7）并行。spike全PASS后先完成WA-06 9.6c/9.6d，再集成pre-build roots/WA-07 3.6a并生成发行候选，候选通过7.4后才运行各owner packaged revalidation，最后由WA-08 frozen journey汇合最终EXE gate。
- **Diagnostic onedir**：可用于hook/resource/Qt布局诊断，但必须标记`NO_AUTHORITY/NOT_FOR_RELEASE`，不得改变WA状态、CapabilityHost authority或release matrix。

| Dispatch | Source phase | Frozen pre-build | Frozen post-build |
|---|---|---|---|
| WA-01 | `2.5a, 2.12a, 5.2a, 5.12a` | manifest/root declarations only | `5.12b` against platform 7.4 candidate |
| WA-02 | startup slice `1.3a, 1.4a, 3.2a`；WA-03 source后完成`4.4a, 4.5a` | bootstrap/import hook declarations only | `1.4b, 4.5b` against platform 7.4 candidate |
| WA-03 | 全部Windows owner implementation与source Project journey | no additional owner task | 由WA-08 packaged Project journey消费复验 |
| WA-04 | `2.4a, 2.5a, 3.4a, 4.4a, 5.4a` | bundle resource declarations only | `5.5a` against platform 7.4 candidate |
| WA-05 | `2.3a, 3.4a, 3.5a, 5.3a` | bundle source declarations only | `5.4a` against platform 7.4 candidate |
| WA-06 | `1.2a, 5.3a, 5.6a～5.14a, 8.8a, 9.1a, 9.2a, 9.6a` | `9.6c`受信Gate输入；`9.6d` fresh worker消费；Gate/TM roots | `9.6b` against platform 7.4 candidate |
| WA-07 | `3.5a, 3.8a, 5.4a, 5.5a, 6.6a, 6.7a, 7.2a, 7.4a, 7.6a` | `3.6a` trusted bootstrap consumption | `6.6b, 7.4b, 7.6b, 9.2a` against platform 7.4 candidate |
| WA-08 | `4.8a, 5.2a, 5.3a, 5.4a` | resource/plugin declarations only | `5.2b, 5.3b, 5.4b` after platform 7.4/7.4a |

ADR-020～026 promotion、Windows owning scope、WA-01～08 current R/D/T delta及owning Spec acknowledgement已批准。WA-03 `R2`依据ADR-025取代`R1`；WA-06 `R3`依据ADR-024/025取代`R2`，2026-09-19人工批准的`R4`沿用Requirements语义并补齐frozen输入/worker的Design/Tasks；WA-01保持`R1`，但按ADR-026明确Parser无状态canonical writer，不新增journal/LKG。amendment commit可达性、evidence和终态disposition仍待后续implementation clusters逐行闭合。任一`CONTRACT_CHANGE`行缺少current approved R/D/T、可达commit或fresh evidence时，对应Windows consumer cluster与最终EXE gate均为 **NO-GO**。

#### Delivery-staging Owner Approval Register

本表只批准source/frozen验收拆分、task依赖与非终态status词汇；不新增或削弱任何WA public contract。该批准与既有`dispatch_request_revision`正交，后续若出现authority、持久格式、publish/recovery或依赖方向增量，仍必须提升对应WA request revision，不能借交付分期吸收。

| dispatch | owning Spec | approved owner-document delta | owner acknowledgement |
|---|---|---|---|
| WA-01 | `parser-subsystem-extraction` | Tasks：source/frozen completion拆分及candidate依赖 | `ACKNOWLEDGED` |
| WA-02 | `collaborative-job-chunks` | Requirements/Design/Tasks/review：startup slice先行，真实ProjectPackage耦合的`4.4a/4.5a`在WA-03 source后完成；frozen candidate依赖不变 | `ACKNOWLEDGED` |
| WA-03 | `multi-document-project-workspace` | Requirements/Tasks：source owner结果与packaged Project复验拆分；current `R2`另由Dispatch Authority Register授权 | `ACKNOWLEDGED` |
| WA-04 | `language-resource-portability` | Requirements/Tasks：source authority与packaged resource revalidation拆分 | `ACKNOWLEDGED` |
| WA-05 | `tmx-context-interchange` | Tasks：source TMX completion与packaged journey拆分 | `ACKNOWLEDGED` |
| WA-06 | `tm-storage-retrieval-index` | Tasks：source TM completion与candidate frozen revalidation拆分；current `R4`新增9.6c/9.6d pre-build消费，另由Dispatch Authority Register授权 | `ACKNOWLEDGED` |
| WA-07 | `feature5-ui-integration` | Requirements/Design/Tasks：source/frozen authority、pre-build bootstrap消费与post-build GO拆分 | `ACKNOWLEDGED` |
| WA-08 | `qt-editor-json-mvp-increment` | Requirements/Design/Tasks：launcher消费、source journey与packaged journey拆分 | `ACKNOWLEDGED` |

### Dispatch Authority Register

`dispatch_id`是task使用的稳定amendment身份；`dispatch_request_revision`只在本register内记录同一dispatch的合同版本（`R1`、`R2`、`R3`、`R4`），不复制进每个task标签。同一WA在不同dispatch group出现时必须复用该身份、owning Spec acknowledgement与ledger row。真实合同增量必须升revision并把旧request保留为`SUPERSEDED`历史，不能原地改写。当前批准只冻结已acknowledged的R/D/T请求，不预填尚未发生的commit、artifact或终态disposition。owning Spec 的`spec.json`只表达该Spec基线文档/实现状态，不能覆盖本ledger登记的current amendment revision；对Windows cluster，授权条件是“基线允许且current `dispatch_request_revision`为`ACKNOWLEDGED`或更后终态”，任何`PENDING`/`SUPERSEDED`/`BLOCKED`均优先阻断对应suffix task、integration与evidence。

| dispatch_id | dispatch_request_revision | owning_spec | owner_acknowledgement | current_status |
|---|---|---|---|---|
| WA-01 | `R1` | `parser-subsystem-extraction` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-02 | `R1` | `collaborative-job-chunks` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-03 | `R2` | `multi-document-project-workspace` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-04 | `R1` | `language-resource-portability` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-05 | `R1` | `tmx-context-interchange` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-06 | `R4` | `tm-storage-retrieval-index` | `ACKNOWLEDGED` | `ACKNOWLEDGED` |
| WA-07 | `R1` | `feature5-ui-integration` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-08 | `R1` | `qt-editor-json-mvp-increment` | `ACKNOWLEDGED` | `ACKNOWLEDGED` |

Superseded request history：WA-03 `R1`曾获acknowledgement，现由采用`WindowsDocumentedPublishV1`且移除运行时硬件registry/power-lab前置的`R2`完整取代；WA-06 `R1`曾因V1不能表达MIC authority由`R2`取代，继而由纳入ADR-024 provider-agnostic主体与ADR-025正常发布边界的`R3`取代。2026-09-19用户批准[修订请求](task7-prebuild-consumption-amendment.md)（提案提交`b48a291ce831d7b3c05e1363105fa84a4636e26a`），Core、平台和Feature5的Design/Tasks/ledger增量共同落盘，WA-06 current升为`R4`；Requirements语义不变，WA-07继续R1。WA-03 `R1`及WA-06 `R1`/`R2`/`R3`标记`SUPERSEDED`；R3 source evidence保留原始anchor与范围，不改签为R4 frozen PASS。R4尚无实现integration anchor或pre-build/runtime完成事实。

## Amendment Dispatch Table

| Dispatch | Owning Spec | Disposition | Requirements delta | Design delta | Proposed amendment tasks | Integration dependency | Required evidence |
|---|---|---|---|---|---|---|---|
| WA-01 | `parser-subsystem-extraction` | `CONTRACT_CHANGE` | 增加 Windows rooted read、canonical writer、reparse/ancestor/final swap、live-handle identity、body-safe failure；保持 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE` fail-closed | Parser 继续拥有 sealed source/writer 与业务错误，改为消费 `RootedFileSystem`/publisher；Windows source 不再用 path/lstat 近似，并按ADR-026保持单文件、无状态canonical writer，不新增journal/LKG | `2.5a` rooted reader port；`2.12a` Windows adversarial read；`5.2a` canonical writer port；`5.12a` source completion；`5.12b` frozen revalidation | source：W1 + Windows contracts；frozen：W3 spike + platform 7.4 candidate | rooted source/writer shared contract；junction/reparse/hardlink/ancestor swap；body unread；source与frozen证据分阶段 |
| WA-02 | `collaborative-job-chunks` | `CONTRACT_CHANGE` | 增加 Windows startup、cross-process lock、publish/kill recovery 与稳定错误 | Chunk owner 保留 journal/LKG/state machine；注入 `ProcessFileLock`/publisher，删除业务顶层 `fcntl`；不取得ProjectPackage physical I/O authority | startup slice：`1.3a` platform contracts、`1.4a` source composition、`3.2a` LockFileEx；WA-03 source后：`4.4a` publish/recovery、`4.5a` source acceptance；frozen：`1.4b/4.5b` | startup：W1 + Windows contracts，是source Qt直接前置；package-coupled source completion：WA-03 `2.8a/4.4a`；frozen：W3 spike + platform 7.4 candidate | startup/import无POSIX module、metadata独立锁/恢复；随后以真实Windows ProjectPackage完成cold-open、双进程、kill、candidate close/reopen与journal/LKG恢复；禁止mock/private ZIP/skip代答 |
| WA-03 | `multi-document-project-workspace` | `CONTRACT_CHANGE` | 增加 Windows ProjectPackage save/reopen、deterministic ZIP、concurrent writer，以及fault/process kill/app restart/正常reboot恢复 | Project owner 保留 ADR-018/019 carrier、receipt、LKG；在owner lease下消费`WindowsDocumentedPublishV1`的write-through/flush、handle-bound naming、retained readback，并在业务commit后terminal reproof | `2.1a` identity/port；`2.4a` bound save；`2.8a` reopen/recovery；`4.3a` hostile Windows paths；`4.4a` process/restart boundary | W1 + ADR-025 + WA-01；可与 WA-04/05并行 | save/reopen byte/digest parity、target-open rejection、uncooperative swap=>recovery、fault/process kill/app restart/正常reboot下完整old/new/recovery-only；不以forced-power lab作success前置 |
| WA-04 | `language-resource-portability` | `CONTRACT_CHANGE` | 增加 resource artifact/package/import/repository/receipt 在 Windows 的 rooted/save/reopen/recovery | Resource owner 保留 portability/receipt semantics；共享 platform port 替代 `_bind_parent`/dirfd/fsync | `2.4a` rooted resource source；`2.5a` resource identity；`3.4a` bound publish；`4.4a` package/repository；`5.4a` Windows hostile matrix；`5.5a` packaged resource evidence | source：W1 + WA-01，可与WA-03/05并行；frozen：platform 7.4 candidate | Resource import/save/reopen、ledger/repository、junction/swap/lock/recovery、frozen data path |
| WA-05 | `tmx-context-interchange` | `CONTRACT_CHANGE` | 增加 Windows rooted TMX source、canonical save、no-target-change failure、restart/packaged import | TMX owner 保留 locale/conflict/receipt semantics；消费 Parser sealed source与 bound publisher | `2.3a` Windows source；`3.4a` bound writer；`3.5a` recovery；`5.3a` hostile source；`5.4a` packaged TMX journey | source：W1 + WA-01，之后与WA-03/04并行；frozen：platform 7.4 candidate | valid count、invalid/escaped/reparse source zero mutation、canonical bytes/reopen、frozen import |
| WA-06 | `tm-storage-retrieval-index` | `CONTRACT_CHANGE` | 保留R3的Windows activation/recovery、FTS5、provider-agnostic private attestation与FileId要求；R4不改变Requirements语义 | 保留Core SQLite/业务authority；按ADR-009/022/023新增受信输入session与fresh worker消费，平台提供manifest-bound reads及E10后固定dispatch，Core保留grammar/digest/Gate/codec/RSS/timeout | 原`a`任务保持；`9.6c`受信输入、`9.6d` fresh worker为pre-build；`9.6b`同候选post-build | source既有依赖不变；pre-build：9.6a + W3 spike；post-build：9.6c/9.6d + platform 7.4 | 保留R3完整产品矩阵；新增真实Gate输入、独立两child、严格IPC、关闭/漂移/terminal reproof；100k双路径硬门不变 |
| WA-07 | `feature5-ui-integration` | `CONTRACT_CHANGE` | 增加 Windows capability composition、TM activation/restart projections、frozen bootstrap/source authority 和 safe diagnostic | Feature5/UI owner 继续拥有 Controller/CapabilityHost；source消费rooted source authority，frozen消费`TrustedSourceAuthority`，两者均不得以path/metadata近似铸造proof | `3.5a` source composition；`3.6a` frozen bootstrap；`3.8a` source lifecycle；`6.6a/6.6b` source/frozen qualification；`7.4a/7.4b` failure projection；`7.6a/7.6b` regression；`9.2a` packaged integration | source：WA-06 source；frozen pre-build `3.6a`依赖W3 spike与WA-06 9.6c/9.6d，其余依赖platform 7.4 + WA-06 frozen；先于WA-08对应阶段 | no `fcntl` startup、source业务E2E；frozen再证明Boot TCB/exact-source/no duplicate；TM safe state/restart、no raw proof/path in UI |
| WA-08 | `qt-editor-json-mvp-increment` | `CONTRACT_CHANGE` | 增加 Windows source/frozen project journey、资源/avatar回归和 qwindows diagnostics | Qt increment继续拥有单JSON journey/resource presentation；source使用source-owned roots，frozen使用trusted bundle root；轻量launcher不取得packaging authority | `4.8a` source save/reopen；`5.2a/5.2b` source/frozen resources；`5.3a/5.3b` source/frozen visible window；`5.4a/5.4b` user-managed/packaged journey | source：WA-03/04/05/07 source + platform 6.6a；frozen：platform 7.4a post-build owner PASS | visible main window、project save/reopen、TMX/import path、avatar catalog索引/解码与fallback、non-repo CWD |
| WR-01 | `tm-store-module-extraction` | `REVALIDATION_ONLY` | 无 public contract delta | 模块拆分边界不拥有平台语义；只重跑 downstream import/authority/recovery regression | 不追加 amendment task；在 Windows final ledger 记录 revalidation evidence | WA-06 integration 后 | module boundary/AST、store reopen、无直接 POSIX/Win32 primitive regression |
| WR-02 | `termbase-column-selection-import` | `REVALIDATION_ONLY` | 无 column selection/import contract delta | Termbase 路径通过 Resource/Parser 端口获得 Windows 能力；本 Spec 不改列映射行为 | 不追加 amendment task；在 WA-04/05 consumer regression 中引用 | WA-01/04 integration 后 | column selection/import success + hostile source zero mutation；无平台专属业务分支 |
| WR-03 | `qt-editor-mvp` / completed baseline | `NO_AMENDMENT` | 无；旧横向 Qt baseline 不重新打开 | 当前 Windows UI 增量由 WA-07/08 拥有 | 不追加 task | WA-07/08 完成后只跑 baseline | offscreen/visible startup、keyboard/accessibility baseline |

WA-04/05 `R1`的owner R/D/T没有registry delta，直接消费ADR-025修订后的平台publish port，不升revision。WA-07/08 `R1`同样保持：frozen manifest移除`DurabilityProfileRegistryV1`/`packaging/windows/durability_profiles.json`输入，其他ADR-022 strict frozen、source closure与packaged journey要求不变。

## Dispatch Groups

### Group A — Startup / Source
- **首先跑通 Qt source startup 的直接阻塞是 WA-02 Chunk**：`qt_editor.py` 启动组合必定加载协作分工模块，而其顶层 `fcntl` import 在 Windows 进入 composition 前即失败。WA-02 先消费 W1 lock/publisher contracts，移除该直接平台依赖并保持 chunk journal/LKG。
- **WA-01 Parser 是紧随其后的 Source/Writer vertical slice**：它不负责消除最初的 `fcntl` import，但项目打开、TMX/resource import 与 canonical writer 都依赖其 Windows rooted authority，因此必须在 persistence amendments 前集成。
- WA-01 与 WA-02 startup slice可以并行；启动验收顺序为 `Windows platform factory → WA-02 import/startup/独立metadata recovery → WA-01 rooted source/writer`。该阶段只将WA-02登记为`SOURCE_COMMITTED`，不声称真实ProjectPackage冷开已经完成。
- WA-07 CapabilityHost 的`R1` request在本组提前派发以冻结source authority接口；Group C只完成同一request，不产生第二dispatch/acknowledgement/row。其source实现依赖WA-06 source阶段而不依赖W3；frozen实现另依赖W3 custom spike，不是最初source Qt import的前置。
- 派发包必须附 W1/W3 reference、rooted/error/source authority delta 和禁止 Windows path-only proof 的反例。

### Group B — Persistence / Recovery
- WA-03 Project、WA-04 Resource、WA-05 TMX、WA-06 TM Core；WA-03真实Windows ProjectPackage完成后，回到同一WA-02 row闭合`4.4a/4.5a` package-coupled source acceptance并推进`SOURCE_MERGED_PASS`。
- 派发包必须附 publish mode/threat scope、`WindowsDocumentedPublishV1`的write-through/flush→handle-bound naming→retained readback→owner commit→terminal reproof、owner-specific envelope + nested W2 V2 proof，以及fault/process kill/app restart/正常reboot下的old/new/recovery-only结果。

### Group C — UI / Staged Delivery Journey
- 完成Group A已登记的同一WA-07 Feature5/UI request，并派发WA-08 Qt increment；WR-01/02/03仅进入revalidation manifest。
- source阶段附rooted source authority、用户管理runtime、qwindows/visible UI与业务journey；frozen阶段另附W3 native-entry pre-Python DLL policy、bootstrap/loader contract、bundle-relative assets与最终clean-user packaged journey。

## Integration Order and Parallelism

```text
W1 + ADR-025 + Steering ownership
  -> Windows platform contracts/backends + POSIX parity
  -> [WA-01S Parser || WA-02 startup slice]
  -> [WA-03S Project || WA-04S Resource || WA-05S TMX]
  -> WA-02 package-coupled source completion
[W2 + ADR-023/024/025 + WA-01S + WA-02 source completion + Windows backend] -> WA-06S TM Core
[WA-06S + source authority] -> WA-07S Feature5/UI -> platform 6.6a launcher -> WA-08S -> platform 6.6b milestone

W3 custom plan || source lane
[platform 2.1 -> 2.2 -> 2.3 -> 2.4 -> 3.1 -> 3.2] -> [W3 custom spike || platform 3.3 -> 3.7]
[W3 custom spike PASS + source owner integrations] -> WA-06 9.6c -> WA-06 9.6d
  -> WA-07 3.6a + pre-build roots -> platform 7.0
  -> manifest/handoff/build -> platform 7.4 candidate
  -> WA-01F/02F/04F/05F/06F + WA-07 post-build revalidation
[WA-03/04/05 + post-build owner PASS] -> WA-08F
  -> WR-01/02/03 revalidation
  -> full onedir/windowed packaged E2E
  -> Windows + macOS/Linux final evidence
```

W3 packaging implementation可在 consumer amendments 期间继续，但 release closure 必须等待 WA-01～08 的批准实现与复验证据全部可达；WA-05 已确认为 `tmx-context-interchange`，后续只在其 R/D/T amendment 未批准时阻塞 TMX cluster。

## Integration Ledger Schema

每个 `WA-*` 完成时追加一行实际事实；当前不预填虚假 commit/approval：

| Field | Required value |
|---|---|
| dispatch_id | 上表稳定 ID |
| dispatch_request_revision | 与`dispatch_id`组成唯一R/D/T request identity的`R1`/`R2`/`R3`/`R4`版本；同一WA跨group不得重发 |
| owning_spec | 人类批准的唯一 Spec/合同 authority；branch/worktree不构成owner或审批身份 |
| owner_acknowledgement | owning Spec对当前request revision的稳定批准状态 |
| current_status | `PENDING` / `ACKNOWLEDGED` / `SOURCE_COMMITTED` / `SOURCE_MERGED_PASS` / `FROZEN_PREBUILD_COMMITTED` / `FROZEN_REVALIDATED_PASS` / `COMMITTED` / `MERGED_PASS` / `BLOCKED` / `SUPERSEDED`；source与两个frozen staged状态均为非终态，只有`MERGED_PASS`是成功终态 |
| requirements_approval | approved revision/commit |
| design_approval | approved revision/commit，含 ADR mapping |
| tasks_approval | approved revision/commit，含 amendment task IDs |
| integration_anchor | 单一稳定commit；其tree包含该row已批准的amendment实现与所声明验收，较早实现提交由Git ancestry追溯；禁止记录工作树hash、patch-equivalent或随分支推进变化的tip |
| disposition | 仅在终态记录`MERGED_PASS` / `BLOCKED` / `SUPERSEDED`，不得用 `SKIPPED` 放行 required row |

### Current Integration Facts

| dispatch_id | revision | owning_spec | integration_anchor | current_status | disposition |
|---|---|---|---|---|---|
| WA-01 | `R1` | `parser-subsystem-extraction` | `da986925f47fe6d18cb0f316c79a70c0655808b6` | `SOURCE_MERGED_PASS` | — |
| WA-02 | `R1` | `collaborative-job-chunks` | `9abd9e1a5942ddffa0dad24a4d41e0f863d57ff1` | `SOURCE_MERGED_PASS` | — |
| WA-03 | `R2` | `multi-document-project-workspace` | `faed77761b39f273563bd39dc51f5e39cd620cf5` | `SOURCE_MERGED_PASS` | — |
| WA-04 | `R1` | `language-resource-portability` | `7e28b2b62a0a1c99aff87a3b055463d169a6c9d3` | `SOURCE_MERGED_PASS` | — |
| WA-05 | `R1` | `tmx-context-interchange` | `7e28b2b62a0a1c99aff87a3b055463d169a6c9d3` | `SOURCE_MERGED_PASS` | — |
| WA-06 | `R3`（历史） | `tm-storage-retrieval-index` | `e7bab7a57283964e9424ea458b222f5b5f65a7fb` | `SOURCE_MERGED_PASS`（原始事实） | `SUPERSEDED` |
| WA-06 | `R4` | `tm-storage-retrieval-index` | — | `ACKNOWLEDGED` | — |
| WA-07 | `R1` | `feature5-ui-integration` | `76f9ea47c7962d2d6d3daaf3c18195dc6afc53e4` | `SOURCE_MERGED_PASS` | — |
| WA-08 | `R1` | `qt-editor-json-mvp-increment` | `8f0a418a6e5fbbd7cfc302c75300a78fbad76680` | `SOURCE_MERGED_PASS` | — |

`WR-*` 记录稳定revalidation anchor、结果和“无 contract delta”的owner确认；完整命令、日志、矩阵与摘要归受版本控制的evidence manifest或受保留CI artifact，不把ignored本机副本写入本ledger。只有ledger全部闭合且实际代码差异仍落在获批边界内，最终Windows Feature GO才可进入审查。
