# Cross-Spec Amendment Dispatch and Integration Ledger

## 当前交付解释（2026-09-20）

source 已由 Task 6.6b 完成独立产品验收。下文 `SOURCE_MERGED_PASS` 是已交付 source 的事实；它不是 frozen 通过，也不因 frozen 仍未达到 `MERGED_PASS` 而变成 source 未完成。

[ADR-028](../../steering/adr/adr-028.md) 部分取代原 W3 的完整 Boot TCB/native entry/source-only 前置；下方旧 W3 映射和 staged vocabulary 保留历史用途。WA-06 R3 source、R4 pre-build 与 WA-07/08 source 成果保留，不重签为 packaged 成果。用户明确批准 `reassessment@3e41130` 的普通 frozen 活动 R/D/T 及已列明相邻 owner 修订，按现有依赖实施；旧 ACK 与原始 evidence 保持原范围，不重签候选或正式 Gate 验收。

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

W1/W2 的用户数据与本机资格保证继续适用。表中 W3 是 ADR-022 原强证明路线的历史映射，ADR-028 已部分取代其 native entry、完整 Boot TCB 与 source-only 发行前置；普通 frozen 的活动依赖以下节及各 owning Design 为准，不能从历史 W3 行恢复旧硬门。

### Source / Frozen Delivery Staging

source 的已批准分期、原 task 和 evidence 保留：`platform 6.6a launcher → WA-08 5.4a → platform 6.6b` 已闭合，不等待 W3。原 staging acknowledgement 与 WA request register 在下方保留其原批准范围。

普通 frozen 的本轮活动依赖如下，用户已明确批准 `reassessment@3e41130` 中全部已列明新增/替换合同。它修订平台 Requirement 10–12 及受影响的 Core/Host/Qt 私有消费接缝，不新设业务 authority，不为组合实现增加一套 revision/profile 制度。R4 仍标识原已批准的 pre-build 合同；本轮实施依据这次明确审批，不能借 R4 的 acknowledgement 改写旧验收。若后续需要改变公开 codec、资格持久格式或业务合同，必须由原 owner 明确提案。

| Dispatch | 已完成基线 | 本轮普通 frozen 消费 / 验收依赖 |
| --- | --- | --- |
| WA-01 | source rooted reader/writer | `5.12b` 用平台 7.2 真实候选验证；不等待 W3 或平台 7.4 汇总先完成 |
| WA-02 | source startup、metadata、ProjectPackage 耦合验收 | `1.4b/4.5b` 用 7.2 候选，保持现有 Chunk/Project authority |
| WA-03 | Windows ProjectPackage 实现与 source journey | 无新增业务任务，7.4/8 与 WA-08 在候选消费并按 Requirement 12 引用/重验深入证据 |
| WA-04 | source resource/package/receipt | `5.5a` 用 7.2 候选；需要 Fuzzy 的消费等待 7.3，本身不授予资格 |
| WA-05 | source TMX、发布与恢复 | `5.4a` 用 7.2 候选，保持 direct import 与 export-only package 边界 |
| WA-06 | R3 source、R4 `9.6c/9.6d` pre-build | 新 `9.6e` 消费 7.2a 实际产物并供 7.2b 集成；`9.6b` 在 7.2 后形成正式资格，供平台 7.3 汇合 |
| WA-07 | source 与 W3 `3.6a` pre-build | 新 `3.6b` 消费 7.2a + Core 9.6e；`6.6b` 消费 Core 9.6b 供 7.3；其余普通 UI/失败验收在 7.4 汇总之前完成 |
| WA-08 | source 用户旅程 | `5.2b/5.3b` 使用 7.2 候选；`5.4b` 汇合 7.3 和 owner 业务结果后供平台 7.4 汇总 |

平台 7.2 是首条生产链的集成完成项，不是 Core/Host 实现的先决完成项：`7.2a → Core 9.6e → Feature5 3.6b → 7.2b/7.2c → 7.2`。后续 `Core 9.6b → Feature5 6.6b → 7.3 → owning journeys → 7.4 → 8/9/10`。任何 owner 不以最终汇总 PASS 作为自身第一次运行的前提。

| 已批准范围（`reassessment@3e41130`） | 唯一 owning 文件 | 本轮状态 |
| --- | --- | --- |
| 普通发行 Requirement 10–12、build/entry/transport、验收复用与任务图 | 本 Spec requirements/design/tasks/spec.json | Requirements / Design / Tasks 已获用户明确批准；候选未验收 |
| 输入、compatibility、oracle/query、worker、publication | Core requirements/design/tasks/trusted-input-publication/spec.json | 已获用户明确批准；R4 原完成事实不变 |
| composition、generation/notification、资格与取消投影 | Feature5 requirements/design/tasks/spec.json | 已获用户明确批准；source 与 3.6a 原范围不变 |
| 普通资源/入口、最终产品 journey | Qt increment requirements/design/tasks/spec.json | 已获用户明确批准；source 不重签 |
| WA-01/02/04/05 的普通候选前置及证据范围 | 各 owning tasks 和必要的原 frozen 来源条款 | 仅依赖/适用范围获用户明确批准；业务保证不变 |

这些是既有 ledger 的本轮审批范围，不是新治理文档。旧 register 的 ACKNOWLEDGED 不能覆盖本表；本表未批准时普通任务及发行保持阻塞，不能以 SKIP 消除。

#### Delivery-staging Owner Approval Register（历史批准范围）

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

### Dispatch Authority Register（既有批准与实现范围）

`dispatch_id` 是 task 使用的稳定 amendment 身份；`dispatch_request_revision` 在本 register 记录已形成的 R1–R4 合同沿革，不复制进每个 task 标签。同一 WA 跨 group 复用同一身份，既有批准和被取代请求保留原范围，不能原地改签。用户已明确批准 `reassessment@3e41130` 中上方范围的 Requirements→Design→Tasks 修订，各 owning `spec.json` 记录当前授权；下表旧 ACK 保留其原审批和完成范围，不代替本次审批或新的执行证据。不为没有公共合同变化的私有接线制造新 revision；若后续确实修改公共合同，则由原 owner 明确版本和取代范围。提交、artifact 和终态 disposition 均只在实际发生后登记。

| dispatch_id | dispatch_request_revision | owning_spec | owner_acknowledgement | current_status |
|---|---|---|---|---|
| WA-01 | `R1` | `parser-subsystem-extraction` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-02 | `R1` | `collaborative-job-chunks` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-03 | `R2` | `multi-document-project-workspace` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-04 | `R1` | `language-resource-portability` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-05 | `R1` | `tmx-context-interchange` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |
| WA-06 | `R4` | `tm-storage-retrieval-index` | `ACKNOWLEDGED` | `FROZEN_REVALIDATED_PASS` |
| WA-07 | `R1` | `feature5-ui-integration` | `ACKNOWLEDGED` | `FROZEN_PREBUILD_COMMITTED` |
| WA-08 | `R1` | `qt-editor-json-mvp-increment` | `ACKNOWLEDGED` | `SOURCE_MERGED_PASS` |

Superseded request history：WA-03 `R1`曾获acknowledgement，现由采用`WindowsDocumentedPublishV1`且移除运行时硬件registry/power-lab前置的`R2`完整取代；WA-06 `R1`曾因V1不能表达MIC authority由`R2`取代，继而由纳入ADR-024 provider-agnostic主体与ADR-025正常发布边界的`R3`取代。2026-09-19用户批准[修订请求](task7-prebuild-consumption-amendment.md)（提案提交`b48a291ce831d7b3c05e1363105fa84a4636e26a`），Core、平台和Feature5的Design/Tasks/ledger增量共同落盘，WA-06 current升为`R4`；Requirements语义不变，WA-07继续R1。WA-03 `R1`及WA-06 `R1`/`R2`/`R3`标记`SUPERSEDED`；R3 source evidence保留原始anchor与范围，不改签为R4 frozen PASS。批准落盘时R4尚无实现integration anchor或pre-build/runtime完成事实；随后Core 9.6c/9.6d及Feature5 3.6a已独立验收并提交，当前pre-build事实见下表，runtime仍未验收。

## Amendment Dispatch Table

| Dispatch | Owning Spec | Disposition | Requirements delta | Design delta | Proposed amendment tasks | Integration dependency | Required evidence |
|---|---|---|---|---|---|---|---|
| WA-01 | `parser-subsystem-extraction` | `CONTRACT_CHANGE` | 增加 Windows rooted read、canonical writer、reparse/ancestor/final swap、live-handle identity、body-safe failure；保持 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE` fail-closed | Parser 继续拥有 sealed source/writer 与业务错误，改为消费 `RootedFileSystem`/publisher；Windows source 不再用 path/lstat 近似，并按ADR-026保持单文件、无状态canonical writer，不新增journal/LKG | `2.5a` rooted reader port；`2.12a` Windows adversarial read；`5.2a` canonical writer port；`5.12a` source completion；`5.12b` frozen revalidation | source：W1 + Windows contracts；frozen：platform 7.2 普通候选（涉及 Fuzzy 的消费另需 7.3） | rooted source/writer shared contract；junction/reparse/hardlink/ancestor swap；body unread；source与frozen证据分阶段 |
| WA-02 | `collaborative-job-chunks` | `CONTRACT_CHANGE` | 增加 Windows startup、cross-process lock、publish/kill recovery 与稳定错误 | Chunk owner 保留 journal/LKG/state machine；注入 `ProcessFileLock`/publisher，删除业务顶层 `fcntl`；不取得ProjectPackage physical I/O authority | startup slice：`1.3a` platform contracts、`1.4a` source composition、`3.2a` LockFileEx；WA-03 source后：`4.4a` publish/recovery、`4.5a` source acceptance；frozen：`1.4b/4.5b` | startup：W1 + Windows contracts，是source Qt直接前置；package-coupled source completion：WA-03 `2.8a/4.4a`；frozen：platform 7.2 普通候选（涉及 Fuzzy 的消费另需 7.3） | startup/import无POSIX module、metadata独立锁/恢复；随后以真实Windows ProjectPackage完成cold-open、双进程、kill、candidate close/reopen与journal/LKG恢复；禁止mock/private ZIP/skip代答 |
| WA-03 | `multi-document-project-workspace` | `CONTRACT_CHANGE` | 增加 Windows ProjectPackage save/reopen、deterministic ZIP、concurrent writer，以及fault/process kill/app restart/正常reboot恢复 | Project owner 保留 ADR-018/019 carrier、receipt、LKG；在owner lease下消费`WindowsDocumentedPublishV1`的write-through/flush、handle-bound naming、retained readback，并在业务commit后terminal reproof | `2.1a` identity/port；`2.4a` bound save；`2.8a` reopen/recovery；`4.3a` hostile Windows paths；`4.4a` process/restart boundary | W1 + ADR-025 + WA-01；可与 WA-04/05并行 | save/reopen byte/digest parity、target-open rejection、uncooperative swap=>recovery、fault/process kill/app restart/正常reboot下完整old/new/recovery-only；不以forced-power lab作success前置 |
| WA-04 | `language-resource-portability` | `CONTRACT_CHANGE` | 增加 resource artifact/package/import/repository/receipt 在 Windows 的 rooted/save/reopen/recovery | Resource owner 保留 portability/receipt semantics；共享 platform port 替代 `_bind_parent`/dirfd/fsync | `2.4a` rooted resource source；`2.5a` resource identity；`3.4a` bound publish；`4.4a` package/repository；`5.4a` Windows hostile matrix；`5.5a` packaged resource evidence | source：W1 + WA-01，可与WA-03/05并行；frozen：platform 7.2 普通候选（涉及 Fuzzy 的消费另需 7.3） | Resource import/save/reopen、ledger/repository、junction/swap/lock/recovery、frozen data path |
| WA-05 | `tmx-context-interchange` | `CONTRACT_CHANGE` | 增加 Windows rooted TMX source、canonical save、no-target-change failure、restart/packaged import | TMX owner 保留 locale/conflict/receipt semantics；消费 Parser sealed source与 bound publisher | `2.3a` Windows source；`3.4a` bound writer；`3.5a` recovery；`5.3a` hostile source；`5.4a` packaged TMX journey | source：W1 + WA-01，之后与WA-03/04并行；frozen：platform 7.2 普通候选（涉及 Fuzzy 的消费另需 7.3） | valid count、invalid/escaped/reparse source zero mutation、canonical bytes/reopen、frozen import |
| WA-06 | `tm-storage-retrieval-index` | `CONTRACT_CHANGE` | 保留数据保护与正式 Gate；普通输入替换旧完整源码来源要求 | Core 拥有 compatibility、session/oracle/query、benchmark/codec 与 publication；消费普通输入映射及 same-EXE 独立 worker | 已完成 source 与 `9.6c/9.6d` 原范围保留；新增 `9.6e` 真实接线，改写 `9.6b` 正式资格 | `7.2a → 9.6e → 7.2`；`7.2 → 9.6b → 7.3` | 实际 Matcher/Gate、取消/关闭/竞争、两 fresh child、原 timing/RSS 与 100k 双路径；未变 owner 证据按 Requirement 12 复用 |
| WA-07 | `feature5-ui-integration` | `CONTRACT_CHANGE` | 普通 composition、TM lifecycle 和 safe diagnostic | 复用唯一 Host、generation/notification 与原子发布；普通入口显式注入，不另建 profile/发布器 | 已完成 source 与 `3.6a` 原范围保留；新增 `3.6b`，修订 `6.6b/7.4b/7.6b/9.2a` | `3.6b` 消费 7.2a + Core 9.6e；`6.6b` 消费 Core 9.6b 供 7.3；其余在 7.4 汇总前完成 | 同候选真实查询/后台工作、取消零发布或完整提交二者之一、唯一 generation/通知、UI 无敏感正文 |
| WA-08 | `qt-editor-json-mvp-increment` | `CONTRACT_CHANGE` | 普通 EXE 用户旅程、资源/avatar 与 qwindows | 保留 Qt presentation owner；从普通 bundle 解析声明资源，无 checkout/CWD fallback | source 项保留；修订 `5.2b/5.3b/5.4b` | `5.2b/5.3b` 消费 7.2；`5.4b` 汇合 7.3 与 owner 旅程，先于平台 7.4 | 真实可见窗口、Project/TM/TMX/FTS5、资源/fallback、无外部 Python、non-repo CWD |
| WR-01 | `tm-store-module-extraction` | `REVALIDATION_ONLY` | 无 public contract delta | 模块拆分边界不拥有平台语义；只重跑 downstream import/authority/recovery regression | 不追加 amendment task；在 Windows final ledger 记录 revalidation evidence | WA-06 integration 后 | module boundary/AST、store reopen、无直接 POSIX/Win32 primitive regression |
| WR-02 | `termbase-column-selection-import` | `REVALIDATION_ONLY` | 无 column selection/import contract delta | Termbase 路径通过 Resource/Parser 端口获得 Windows 能力；本 Spec 不改列映射行为 | 不追加 amendment task；在 WA-04/05 consumer regression 中引用 | WA-01/04 integration 后 | column selection/import success + hostile source zero mutation；无平台专属业务分支 |
| WR-03 | `qt-editor-mvp` / completed baseline | `NO_AMENDMENT` | 无；旧横向 Qt baseline 不重新打开 | 当前 Windows UI 增量由 WA-07/08 拥有 | 不追加 task | WA-07/08 完成后只跑 baseline | offscreen/visible startup、keyboard/accessibility baseline |

既有 ADR-025 publish port 的批准和实现保持原范围。本轮普通 frozen 由上表及 owning R/D/T 单独记录 `reassessment@3e41130` 的用户明确审批；普通入口、输入和 worker 获准实施，但既有 R1/R4 acknowledgement 不被改签为普通候选验收。ADR-022 已被取代的来源要求不再是普通旅程的前置。

## Dispatch Groups

### Group A — Startup / Source
- **首先跑通 Qt source startup 的直接阻塞是 WA-02 Chunk**：`qt_editor.py` 启动组合必定加载协作分工模块，而其顶层 `fcntl` import 在 Windows 进入 composition 前即失败。WA-02 先消费 W1 lock/publisher contracts，移除该直接平台依赖并保持 chunk journal/LKG。
- **WA-01 Parser 是紧随其后的 Source/Writer vertical slice**：它不负责消除最初的 `fcntl` import，但项目打开、TMX/resource import 与 canonical writer 都依赖其 Windows rooted authority，因此必须在 persistence amendments 前集成。
- WA-01 与 WA-02 startup slice可以并行；启动验收顺序为 `Windows platform factory → WA-02 import/startup/独立metadata recovery → WA-01 rooted source/writer`。该阶段只将WA-02登记为`SOURCE_COMMITTED`，不声称真实ProjectPackage冷开已经完成。
- WA-07 的 source composition 已完成；普通 frozen 由已批准的 `3.6b` 消费平台 7.2a 与 Core 9.6e，复用现有 Host 生命周期，不依赖 W3 custom spike。
- 后续派发附实际涉及的 W1/W2、ADR-028 与 owning Design，以及用户数据禁止 path-only proof 的反例。

### Group B — Persistence / Recovery
- WA-03 Project、WA-04 Resource、WA-05 TMX、WA-06 TM Core；WA-03真实Windows ProjectPackage完成后，回到同一WA-02 row闭合`4.4a/4.5a` package-coupled source acceptance并推进`SOURCE_MERGED_PASS`。
- 派发包必须附 publish mode/threat scope、`WindowsDocumentedPublishV1`的write-through/flush→handle-bound naming→retained readback→owner commit→terminal reproof、owner-specific envelope + nested W2 V2 proof，以及fault/process kill/app restart/正常reboot下的old/new/recovery-only结果。

### Group C — UI / Staged Delivery Journey
- 完成Group A已登记的同一WA-07 Feature5/UI request，并派发WA-08 Qt increment；WR-01/02/03仅进入revalidation manifest。
- source 阶段的证据保留原范围；普通 frozen 附候选一致性、Core compatibility/session、same-EXE worker、bundle-relative assets 与最终 clean-user packaged journey。证据复用和变更触发重验按 Requirement 12 执行。

## Integration Order and Parallelism

```text
W1 + ADR-025 + Steering ownership
  -> Windows platform contracts/backends + POSIX parity
  -> [WA-01S Parser || WA-02 startup slice]
  -> [WA-03S Project || WA-04S Resource || WA-05S TMX]
  -> WA-02 package-coupled source completion
[W2 + ADR-023/024/025 + WA-01S + WA-02 source completion + Windows backend] -> WA-06S TM Core
[WA-06S + source authority] -> WA-07S Feature5/UI -> platform 6.6a launcher -> WA-08S -> platform 6.6b milestone

普通 R/D/T 与 owning 消费修订获批
  -> platform 7.2a 普通 EXE / 输入记录
  -> Core 9.6e + Feature5 3.6b -> platform 7.2b/7.2c -> 7.2 真实生产链
  -> Core 9.6b + Feature5 6.6b -> platform 7.3 本机资格
  -> owning packaged API / Qt journeys -> platform 7.4 同候选旅程
  -> 8 候选数据与失败反例 -> 9 触发回归 / 证据复用 / 独立评审 -> 10.1 最终裁决
```

同一最终候选的实际消费不可复用旧 source/旧 EXE 片段；未变底层 owner 的深入矩阵仅按 Requirement 12.5–12.7 引用。普通任务不依赖 W3 custom spike、E10 或完整 Boot TCB；source 图及其历史结果保持原范围。

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

### 原始 source 验收锚

下表保留原source验收登记及其原始anchor。Task 7.0发现这些旧commit仍存在于保留历史，但均不是当前接受链的祖先；不能再把本表直接当作当前可达性证明。下方另列真实可达的集成锚，不修改旧evidence中的commit或摘要。

| dispatch_id | revision | owning_spec | integration_anchor | current_status | disposition |
|---|---|---|---|---|---|
| WA-01 | `R1` | `parser-subsystem-extraction` | `da986925f47fe6d18cb0f316c79a70c0655808b6` | `SOURCE_MERGED_PASS` | — |
| WA-02 | `R1` | `collaborative-job-chunks` | `9abd9e1a5942ddffa0dad24a4d41e0f863d57ff1` | `SOURCE_MERGED_PASS` | — |
| WA-03 | `R2` | `multi-document-project-workspace` | `faed77761b39f273563bd39dc51f5e39cd620cf5` | `SOURCE_MERGED_PASS` | — |
| WA-04 | `R1` | `language-resource-portability` | `7e28b2b62a0a1c99aff87a3b055463d169a6c9d3` | `SOURCE_MERGED_PASS` | — |
| WA-05 | `R1` | `tmx-context-interchange` | `7e28b2b62a0a1c99aff87a3b055463d169a6c9d3` | `SOURCE_MERGED_PASS` | — |
| WA-06 | `R3`（历史） | `tm-storage-retrieval-index` | `e7bab7a57283964e9424ea458b222f5b5f65a7fb` | `SOURCE_MERGED_PASS`（原始事实） | `SUPERSEDED` |
| WA-07 | `R1` | `feature5-ui-integration` | `76f9ea47c7962d2d6d3daaf3c18195dc6afc53e4` | `SOURCE_MERGED_PASS` | — |
| WA-08 | `R1` | `qt-editor-json-mvp-increment` | `8f0a418a6e5fbbd7cfc302c75300a78fbad76680` | `SOURCE_MERGED_PASS` | — |

### 当前继承的 source／构建前基线

下表列出当前继承的 source 与构建前消费基线。代码、有效测试和批准合同继续保留；原 W3 验收报告归强证明路线，不作为普通发行的当前验收。表中状态不表示重新运行 source，也不授予普通 frozen 资格。

| dispatch_id | revision | owning_spec | integration_anchor | current_status | disposition |
|---|---|---|---|---|---|
| WA-01 | `R1` | `parser-subsystem-extraction` | `3960cdc0a746cc42b7637c09f0ed5f33c06677c9` | `SOURCE_MERGED_PASS` | — |
| WA-02 | `R1` | `collaborative-job-chunks` | `f1a8cc514ac579ac485e3c730033221d0ab2ff9e` | `SOURCE_MERGED_PASS` | — |
| WA-03 | `R2` | `multi-document-project-workspace` | `e2f0ce9d887606c49c57881353bbf0660f1543e7` | `SOURCE_MERGED_PASS` | — |
| WA-04 | `R1` | `language-resource-portability` | `d5e6d642451be2280c2af191c110f4e971f4f33b` | `SOURCE_MERGED_PASS` | — |
| WA-05 | `R1` | `tmx-context-interchange` | `d5e6d642451be2280c2af191c110f4e971f4f33b` | `SOURCE_MERGED_PASS` | — |
| WA-06 | `R4` | `tm-storage-retrieval-index` | `f5d559b33dba0ac9265518c416adeb812f980a81` | `FROZEN_PREBUILD_COMMITTED` | — |
| WA-07 | `R1` | `feature5-ui-integration` | `cf8ce140fb95f6c4648623ccd53884b01c60b99f` | `FROZEN_PREBUILD_COMMITTED` | — |
| WA-08 | `R1` | `qt-editor-json-mvp-increment` | `dbd8d558f47c0d43a499ff1ed234ccceb15be295` | `SOURCE_MERGED_PASS` | — |

WA-06原R3事实的当前可达tree为`db7e7ef0024d297264fbc7434b28ec7f77adf96c`，WA-07原source事实为`3d3fa8f74503eebd4e1ef433c2462d151c33e1fd`；前者继续只属`SUPERSEDED`历史，后者只作source追溯。原锚`e7bab7a57283964e9424ea458b222f5b5f65a7fb`及`76f9ea47c7962d2d6d3daaf3c18195dc6afc53e4`保持在历史表与原evidence中，均不改签为新revision/runtime结果。

#### Task 7.0 pre-build 集成范围

以上两个 `FROZEN_PREBUILD_COMMITTED` 锚只声明 Core 9.6c/9.6d 与 Feature5 3.6a 已实现的构建前消费范围。批准依据仍为各 owner 的 Requirements/Design/Tasks、current request acknowledgement 和 R4 的[前置消费修订](task7-prebuild-consumption-amendment.md)。Core 私有发布接口见[受信输入与发布合同](../tm-storage-retrieval-index/trusted-input-publication.md)；普通发行须重新绑定实际执行产物、worker 与输入来源。

历史 owner 声明位于 [`packaging/windows/frozen_roots.json`](../../../packaging/windows/frozen_roots.json)，包含 Gate roots、benchmark inventory、动态 import、worker 映射及默认资源。7.0/7.1 的代码与[构建接口](frozen-build-inputs.md)保留原用途；普通发行只复用有实际消费者的输入，其活动接线以当前 Design 和 Core owning 合同为准，不默认携带该声明的完整源码闭包。

WA-01/02/03/04/05/08 原 source 证据及历史锚保持不变。平台 7.2a–c/7.2、Core 9.6e 与 Feature5 3.6b 的首条实际普通生产链保留原范围，其小样本结果不授予正式资格。正式本机资格与 Core 产品消费由下方 WA-06 已验收候选事实登记；其余 owner 的 clean-user 汇合及平台 `7.4 → 8/9/10` 仍待闭合，没有 terminal `MERGED_PASS`。

### 普通候选集成事实

| dispatch_id | revision | owning_spec | integration_anchor | current_status | disposition |
|---|---|---|---|---|---|
| WA-06 | `R4` | `tm-storage-retrieval-index` | `42fc582421c6872ef2c130c9f3bbe7a4952e8e4b` | `FROZEN_REVALIDATED_PASS` | — |

该锚包含普通候选 `8faeee03e1dd` 的 Core 9.6b、Feature5 6.6b/7.6b 与平台 7.3 本机验收，实际结果及未变 owner 的有限复用见[候选验收范围](#普通候选已验收范围)。本行只推进 WA-06；WA-07/08 的 clean-user 与独立无 Python/Qt 环境仍缺，source/pre-build 原锚及失败事实保留，不将本机验收扩大为发行通过。

### 普通候选已验收范围

验收按实际候选保留范围，已勾选任务不自动覆盖后续产物。原逐项结果、执行命令、环境、日志摘要与失败记录保留在 Git 标签 [`evidence/ordinary-frozen-c462dd0`](https://github.com/BeFringe/LocalCAT/tree/evidence/ordinary-frozen-c462dd0)；原构建提交 `8589e19` 和 `4d8276f` 由既有标签 [`evidence/ordinary-frozen-e3e731b`](https://github.com/BeFringe/LocalCAT/tree/evidence/ordinary-frozen-e3e731b) 保留。历史摘要不是运行时 Gate、资格或发布 authority。本节只登记集成结论，原始过程材料继续留在忽略的 `artifacts/windows/`，不逐次追加为产品规格或新验收门。

#### 初始候选 3c20

候选 `3c20cf46a8a4b6ddbbcaa2adbcf75fc970896619fbadcb26e69b3c42b68714df` 来自原构建提交 `8589e19`；Core fingerprint 为 `d3d200cdbbc431ff9f4ea8f25bae833e890496394c30d96dbda85111c1a73d89`。这是普通 PyInstaller onedir/windowed 与 same-EXE worker 的实际验收，不是 source 结果改签。

| Owning task | 该候选已验收范围 |
| --- | --- |
| Parser `5.12b` | TXT/JSON 打开、canonical 保存与冷开；TMX rooted/reparse 拒绝且用户数据不变。 |
| Chunk `1.4b/4.5b` | 真实 ProjectPackage 两个 chunk 的保存、冷开、metadata 锁及空闲中断恢复；identity/membership/revision 保持。 |
| Resource `5.5a` | Termbase ResourcePackage 导出、校验、预览、创建及明选替换后冷开；TM 快照导出、占用目标拒绝；JSONL 搬运不带资格。 |
| TMX `5.4a` | 直接导入及 managed/project/selected-chunk 三种导出；恶意 XML、reparse、占用目标、package import 拒绝。占用后保留 journal/LKG/stage 并持续 `TMX.RECOVERY_REQUIRED`，未声明恢复成功。 |
| Feature5 `7.4b` | 输入/资格失败、异常 worker、关闭及取消的安全 UI 投影；深层 transport 反例与 GUI 观察分别保留原范围。 |
| Qt increment `5.2b/5.3b` | 正常窗口、声明资源、缺失/损坏 qwindows 入口失败、错误项目及退出；项目工具、预处理应用/撤销、规则冷开和术语 CRUD。 |

该候选曾实际执行 Core Gate 与 100k 双路径计量并发布本机资格：FTS5/fallback recall 均为 1，Exact P95 为 1.1434/1.1822 ms、Fuzzy P95 为 371.0043/384.2717 ms、迁移为 75.740321/111.5683844 s、RSS 为 412.07421875/419.875 MiB。原 qualification SHA-256 为 `dbafdbe8fcdee322b9a8dd2726faf13d58e2964bb2f42f0906cfbff7d9c0b6f3`；这些数值不授予后续候选资格。

未变 owner 的 source 复用限定在原 Windows 持久化及 Qt 深入矩阵；前提、变更接缝与摘要见原 `owner-reuse-audit.json`（SHA-256 `eb6ddb688841c20fbe88afdfe9da13522ce654104e63471a7d487cd304c111b2`）。原始任务、source 锚和失败记录不变，后续 Core/worker 改动须重验受影响消费；本次不声明最终发行通过。

#### 已验收候选 8fae

候选 `8faeee03e1dd31d1162ce1d3447602a7af09fd1b20ac3313fa19bb321f51f5ad` 来自原构建提交 `4d8276f90417927bf329b879239cb9722238b131`；Core fingerprint 为 `4912f458ddf5c009683d319dd97bec959cc6f1b6ec4a4ae3367b102d93c836ab`。后续测试与文档修订未改变产物。原构建血缘由保留标签追溯，不以整理后的提交重签构建或资格。

交付候选为 `LocalCAT-0.5.2-candidate-8faeee03e1dd-windows-x64.zip`（55,606,410 bytes、189 files），SHA-256 `8080c25542e60617788a02e06258ed6ddae18af181f61dabaf39210449bb8951`；EXE SHA-256 `7a60477e75aa9e1295d650382e7147abb89090559691e310fd0e8ea12a0e0de6`。ZIP 与原 payload 逐文件一致，不携带设备密钥或资格。

| 实际能力 | 该候选结果与边界 |
| --- | --- |
| Core 与 Host 正式资格 | 正常设置入口实际执行 Gate C、5000/200 oracle 与 100k FTS5/fallback Gate D，经 same-EXE fresh worker 和既有 Host 发布；冷开恢复、Fuzzy 建议应用及保存真实完成。 |
| TM 导出→激活→查询 | 2929 条 JSONL 搬运后按原顺序、内容和 provenance 激活、冷开查询；不复制数据库、设备密钥或资格。导出目标占用与 W1 锁拒绝保持旧数据，解除后重试成功。 |
| 资格与资源隔离 | 缺失、损坏、不兼容及 ACL 异常资格只关闭 Fuzzy，Exact 保持；canonical ACL 拒绝及 private junction 均不回落 activated JSONL，健康资源可用，恢复后 Exact 可用且数据不变。 |
| 后台工作与关闭 | 实际运行中关闭窗口，父进程 exit 0、子进程回收、无资格安装、数据不变；采集器 WinError 5 与缺少最终 query exit code 的观察限制保留。当前 worker 的 16 个协议/异常反例与未变旧候选 GUI 异常投影有限复用，未声称新跑完整 GUI kill 矩阵。 |
| Project/TMX/Resource 用户旅程 | 两文档六段 Project、两条 TMX 的 Exact/Context 消费、应用保存与中断后冷开；Termbase package 导出/预览/创建/应用/冷开；TMX package import 拒绝且零变更。 |
| 普通入口与资源 | 仓库外 CWD 正常 EXE 启动；候选副本中声明的 terms.csv 缺失/损坏导致稳定失败、exit 1 且用户数据不变。qwindows 负向矩阵仅复用未变 DLL/入口机制；该候选实际加载窗口。 |

该候选登记的最后一次完整计量为单次 `measured_repeats=1`，沿用原 warmup、计时与 child-lifetime RSS 口径；不是稳定裕量保证：

| 路径 | Recall | Exact P95 ms | Fuzzy P95 ms | 迁移 s | RSS MiB |
| --- | --- | --- | --- | --- | --- |
| FTS5 | 1 | 1.1351 | 397.5090 | 90.0511577 | 410.015625 |
| fallback | 1 | 1.1550 | 399.7494 | 119.2563215 | 419.21484375 |

原门限为 50 ms / 500 ms / 120 s / 512 MiB。该次实际通过并发布 qualification `9513d55dbe9fa3cbeb42e27aa0b8841032b8f8a52081f15c4333c7a908afbff2`、bundle `b8b10ad8d5f539033eb4f8b570a3e5bc0a36d53ce374aa54c4e42fcf6398f05e`。同候选此前 fallback 迁移 **137.0997523 s 失败**仍有效；后一次通过不证明波动原因，也不抹去失败。119.2563215 s 距门限仅约 0.744 s。

未变数据端口、SID/MIC 深层矩阵和 Qt 资源算法按 Requirement 12 有限复用；候选入口、Core fingerprint、worker、导出/激活接缝已实际重验。长自定义 data 路径在 `LongPathsEnabled=0` 时曾触发 WinError 206 并进入 `CHUNK.RECOVERY_REQUIRED`；短路径同候选成功只收束测试配置，不承诺任意长路径可用。

受影响 sealer 验证 18 项通过并有 RED 基线；Windows 相关回归修正陈旧 NO_FACTS 观察点后 74 项通过、无 skip，原 73 过/1 失败保留。实际 Mac 回归在 CPython 3.14.7、macOS 26.2 arm64、SQLite 3.53.1 上为 148 过、6 项 Windows-only skip、0 失败（154 methods）；不外推 Linux 或全仓库 PASS。

原验收记录中可复核的关键报告及 SHA-256：`performance-recheck-8fae/result.json` → `96b6e59f7077ab7e2060349b31ab83d3de190c4d4f538187620a39c6e0486e37`；`product-journey-8fae-01/product-journey-result.json` → `c8f07f7034b59a7a53409d576579bb06d4940764b533f5cea5da601190b3fe61`；`core-host-reuse-8fae.json` → `b1afe7ca305079ea1db426ea5c06b27764a62e1f49d05473fef9b710c34f8381`；`acceptance-review-8fae.json` → `feba193c85731f4189e6af9196e98a0ebf31213a5e97cb4c75e9acf2d6a95250`。完整路径、其余报告和历史失败按上方保留 Git 记录追溯，不再维护另一份滚动汇总文件。

已完成范围为 Core `9.6b`、Feature5 `6.6b/7.6b` 与平台 `7.3`，独立累计评审支持此范围。本机净化环境不等同独立未安装 Python/Qt 的 Windows 环境；Feature5 `9.2a`、Qt `5.4b` 与平台 `7.4/8/9/10` 仍待收束，发行状态保持 `NOT_VERIFIED`。后续只补缺失环境及实际变更触发项，不机械重跑已完成矩阵。

`WR-*` 记录稳定revalidation anchor、结果和“无 contract delta”的owner确认；完整命令、日志、矩阵与摘要归受版本控制的evidence manifest或受保留CI artifact，不把ignored本机副本写入本ledger。只有ledger全部闭合且实际代码差异仍落在获批边界内，最终Windows Feature GO才可进入审查。
