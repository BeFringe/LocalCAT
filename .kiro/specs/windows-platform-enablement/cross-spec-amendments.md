# Cross-Spec Amendment Dispatch Ledger

## Purpose and Authority

本文件是 `windows-platform-enablement` 的派发/合并计划，不是相邻 Spec 的就地修订。依据 `.kiro/steering/spec-ownership.md`，每个 amendment 必须在其 owning branch 修改 Requirements/Design/Tasks、独立获批并形成可追踪提交；Windows 分支只记录请求、依赖、merge identity 与最终 evidence。表中 task ID 是拟追加到现有 task 后的 amendment ID，采用 `x.ya`，不重排或冒充原 checkbox。

### Prefix and Disposition Vocabulary

- **`WA-*` — Windows Compatibility Amendment**：Windows 平台兼容性导致 owning Spec 的 public requirement、design boundary 或 implementation task 发生真实增量，必须补发并批准 R/D/T amendment。
- **`WR-*` — Windows Compatibility Revalidation**：既有 feature/Spec 不需要改变合同或追加实现，只需在 Windows consumer 合并后取得 fresh regression evidence。
- **`NO_AMENDMENT`**：该 feature 是已完成基线、由其他 amendment 覆盖或不属于本 Spec 的新增范围，因此既不修改 R/D/T，也不新增 task；只在最终矩阵重验适用行为。
- **`CONTRACT_CHANGE` / `REVALIDATION_ONLY` / `NO_AMENDMENT`** 是 disposition；`WA`/`WR` 是 ledger ID 前缀，两者不能互相替代。

### W1 / W2 / W3 Working Candidates

`W1`～`W3` 是本 Spec 使用的 **Windows ADR candidate working labels**，不是正式 ADR 编号。用户已在 2026-08-25 将三者批准为 `APPROVED_FOR_BASELINE_NAMING`：可作为稳定候选引用、依赖图和派发标识；该批准不等于正式 ADR 已采纳，也不授权平台实现。Governance owner 后续依据 ADR template 形成正式文档和编号。

| Working label | Full candidate name | One-sentence contract | Dependency / References |
|---|---|---|---|
| **W1** | **Cross-platform Rooted File Authority, Lock and Durable Publish Boundary** | 建立共享平台文件合同，并固定 Windows live-handle rooted identity、reparse/share profiles、`LockFileEx`、两种 publish mode、rename→close→reopen 与本地 NTFS durability/error boundary。 | 基础候选；约束所有 WA。参考 [research.md § Governance Candidates](research.md#governance-candidates)、[design.md § Windows Native Adapter](design.md#windows-native-adapter)、[ADR-008](../../steering/adr/adr-008.md)、[ADR-009](../../steering/adr/adr-009.md)、[ADR-018](../../steering/adr/adr-018.md)、[ADR-019](../../steering/adr/adr-019.md) 与 [governance.md](../../settings/rules/governance.md)。 |
| **W2** | **Windows Device-local Private Attestation Representation** | 把 POSIX private/identity proof 映射为 TokenUser/owner SID、精确 DACL/AccessCheck、live FileId 与绑定 digest/phase/device-secret 的跨重启 receipt。 | 依赖 W1 的 identity/error contracts；仅 WA-06/07 直接依赖。参考 [design.md § Private Storage Proof](design.md#private-storage-proof)、[ADR-013](../../steering/adr/adr-013.md)、[ADR-016](../../steering/adr/adr-016.md)。 |
| **W3** | **Windows Frozen Distribution Ownership and Source Closure** | 建立 onedir/windowed build owner、embedded bootstrap trust root、`TrustedSourceAuthority`、真实 `.py`/fixture closure 与 packaged release gate。 | Governance review可与W1并行；bootstrap实现依赖W1 rooted proof，WA-07/08与最终打包依赖W3。参考 [design.md § Frozen Distribution Design](design.md#frozen-distribution-design)、[ADR-009](../../steering/adr/adr-009.md)、[ADR-011](../../steering/adr/adr-011.md)及[research.md § Frozen-source](research.md#frozen-source-与-pyinstaller)。 |

依赖主线是 `W1 → Windows platform contracts/backends → WA consumers`；`W2` 在 TM private attestation 前闭合；`W3` 先做最小 frozen spike，再在 WA-07/08 合并后闭合完整发行物。若正式 ADR 对 working candidate 作拆分或合并，本 ledger 必须记录 superseding mapping，不能继续把 W1/W2/W3 当隐含批准。

W1/W2/W3 的临时命名基线已经批准；正式 ADR promotion、Windows owning scope、相邻 owner 和本 ledger 仍为 `PENDING`。任一 `CONTRACT_CHANGE` 行缺少 approved R/D/T、commit 或可达 merge 时，对应 Windows implementation cluster 与最终 EXE gate 均为 **NO-GO**。

## Amendment Dispatch Table

| Dispatch | Owning Spec / Branch | Disposition | Requirements delta | Design delta | Proposed amendment tasks | Merge dependency | Required evidence |
|---|---|---|---|---|---|---|---|
| WA-01 | `parser-subsystem-extraction` / `parser-rebaseline` | `CONTRACT_CHANGE` | 增加 Windows rooted read、canonical writer、reparse/ancestor/final swap、live-handle identity、body-safe failure；保持 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE` fail-closed | Parser 继续拥有 sealed source/writer 与业务错误，改为消费 `RootedFileSystem`/publisher；Windows source 不再用 path/lstat 近似 | `2.5a` rooted reader port；`2.12a` Windows adversarial read；`5.2a` canonical writer port；`5.12a` publish/recovery matrix | W1 + Windows contracts；先于 Project/Resource/TMX 和 packaged import | rooted source/writer shared contract；junction/reparse/hardlink/ancestor swap；body unread；source + frozen Parser evidence |
| WA-02 | `collaborative-job-chunks` / `feature/collaborative-job-chunks` | `CONTRACT_CHANGE` | 增加 Windows startup、cross-process lock、publish/kill recovery 与稳定错误 | Chunk owner 保留 journal/LKG/state machine；注入 `ProcessFileLock`/publisher，删除业务顶层 `fcntl` | `1.3a` platform contracts；`1.4a` import/composition；`3.2a` LockFileEx；`4.4a` publish/recovery；`4.5a` two-process/kill matrix | W1 + Windows contracts；可与 WA-01 并行，WA-02 是 source Qt import/startup 的直接前置 | controller import 无 POSIX module；双进程竞争、TerminateProcess 后释放、candidate close/reopen、journal/LKG recovery |
| WA-03 | `multi-document-project-workspace` / `feature/multi-document-project-workspace` | `CONTRACT_CHANGE` | 增加 Windows ProjectPackage save/reopen、deterministic ZIP、concurrent writer、crash/reboot recovery | Project owner 保留 ADR-018/019 carrier、receipt、LKG；本地 dirfd helper 改用 platform authority/publisher，replace 只在 owner lease 下 | `2.1a` identity/port；`2.4a` bound save；`2.8a` reopen/recovery；`4.3a` hostile Windows paths；`4.4a` process/power boundary | W1 + WA-01；可与 WA-04/05 并行 | save/reopen byte/digest parity、target-open rejection、uncooperative swap=>recovery、deterministic carrier、old/new/recovery-only |
| WA-04 | `language-resource-portability` / `feature/language-resource-portability` | `CONTRACT_CHANGE` | 增加 resource artifact/package/import/repository/receipt 在 Windows 的 rooted/save/reopen/recovery | Resource owner 保留 portability/receipt semantics；共享 platform port 替代 `_bind_parent`/dirfd/fsync | `2.4a` rooted resource source；`2.5a` resource identity；`3.4a` bound publish；`4.4a` package/repository；`5.4a` Windows hostile matrix；`5.5a` packaged resource evidence | W1 + WA-01；可与 WA-03/05 并行 | Resource import/save/reopen、ledger/repository、junction/swap/lock/recovery、frozen data path |
| WA-05 | `tmx-context-interchange` / **owner branch pending Governance** | `CONTRACT_CHANGE` | 增加 Windows rooted TMX source、canonical save、no-target-change failure、restart/packaged import | TMX owner 保留 locale/conflict/receipt semantics；消费 Parser sealed source与 bound publisher | `2.3a` Windows source；`3.4a` bound writer；`3.5a` recovery；`5.3a` hostile source；`5.4a` packaged TMX journey | Governance 先确定唯一 owner；W1 + WA-01，之后与 WA-03/04 并行 | valid count、invalid/escaped/reparse source zero mutation、canonical bytes/reopen、frozen import |
| WA-06 | `tm-storage-retrieval-index` / `feature5` | `CONTRACT_CHANGE` | 增加 Windows initial activation、single authority、restart recovery、FTS5 reopen、private attestation、FileId reuse 与 power-cut结果 | Core 保留 SQLite authority/generation/reservation/journal/LKG；消费 lock/private/rooted/publish ports；persistent receipt 绑定 digest/phase/private/device-secret，不把 FileId 当永久身份 | `1.2a` platform identity schema；`5.3a` reservation；`5.6a` private storage；`5.7a` publish；`5.8a` restart；`5.9a` FileId reuse；`5.12a`/`5.13a`/`5.14a` fault/power/adversarial；`8.8a` FTS5 reopen；`9.1a`/`9.2a`/`9.6a` activation/public regression | W1 + W2 + WA-01/02 + Windows primitives；先于 WA-07 | initial activation/restart/two-process/kill/power-cut；ACL token matrix；tamper/recreate；FTS5/trigram create/query/reopen |
| WA-07 | `feature5-ui-integration` / `ui-mvp` | `CONTRACT_CHANGE` | 增加 Windows capability composition、TM activation/restart projections、frozen bootstrap/source authority 和 safe diagnostic | Feature5/UI owner 继续拥有 Controller/CapabilityHost；`capability_host` 消费 `TrustedSourceAuthority`，不得以 `Path.resolve/lstat/O_NOFOLLOW=0` 铸造 Windows source proof | `3.5a` Windows platform composition；`3.6a` frozen bootstrap consumption；`3.8a` resource lifecycle；`5.4a`/`5.5a` activation/restart；`6.6a`/`6.7a` W2 re-attestation；`7.2a`/`7.4a`/`7.6a` Qt state；`9.2a` packaged integration | W3 early spike + WA-06；先于 WA-08 final journey | no `fcntl` startup、bootstrap before import、loader/origin/co_filename、TM safe state/restart、no raw proof/path in UI |
| WA-08 | `qt-editor-json-mvp-increment` / `ui-mvp` | `CONTRACT_CHANGE` | 增加 Windows source/frozen project journey、bundle resources、avatar功能回归和 qwindows diagnostics | Qt increment 继续拥有单 JSON editor journey/resource presentation；从 bundle authority 解析发行数据，不依赖 checkout/CWD | `4.8a` Windows save/reopen journey；`5.2a` frozen resource roots/avatar fallback；`5.3a` qwindows/visible window；`5.4a` clean-user packaged journey | WA-03/04/05/07 + full packaging；最后一个 consumer amendment | visible main window、project save/reopen、TMX/import path、avatar catalog索引/解码与无匹配头像fallback、non-repo CWD |
| WR-01 | `tm-store-module-extraction` / `maintenance/tm-store-module-extraction` | `REVALIDATION_ONLY` | 无 public contract delta | 模块拆分边界不拥有平台语义；只重跑 downstream import/authority/recovery regression | 不追加 amendment task；在 Windows final ledger 记录 revalidation evidence | WA-06 merge 后 | module boundary/AST、store reopen、无直接 POSIX/Win32 primitive regression |
| WR-02 | `termbase-column-selection-import` / owner branch按现行治理 | `REVALIDATION_ONLY` | 无 column selection/import contract delta | Termbase 路径通过 Resource/Parser 端口获得 Windows 能力；本 Spec 不改列映射行为 | 不追加 amendment task；在 WA-04/05 consumer regression 中引用 | WA-01/04 merge 后 | column selection/import success + hostile source zero mutation；无平台专属业务分支 |
| WR-03 | `qt-editor-mvp` / completed baseline | `NO_AMENDMENT` | 无；旧横向 Qt baseline 不重新打开 | 当前 Windows UI 增量由 WA-07/08 拥有 | 不追加 task | WA-07/08 完成后只跑 baseline | offscreen/visible startup、keyboard/accessibility baseline |

## Dispatch Groups

### Group A — Startup / Source
- **首先跑通 Qt source startup 的直接阻塞是 WA-02 Chunk**：`qt_editor.py` 启动组合必定加载协作分工模块，而其顶层 `fcntl` import 在 Windows 进入 composition 前即失败。WA-02 先消费 W1 lock/publisher contracts，移除该直接平台依赖并保持 chunk journal/LKG。
- **WA-01 Parser 是紧随其后的 Source/Writer vertical slice**：它不负责消除最初的 `fcntl` import，但项目打开、TMX/resource import 与 canonical writer 都依赖其 Windows rooted authority，因此必须在 persistence amendments 前合并。
- WA-01 与 WA-02 的 owner实现可以并行；启动验收顺序为 `Windows platform factory → WA-02 import/startup → WA-01 rooted source/writer`。
- WA-07 CapabilityHost 的 request 在本组提前派发以冻结 source authority接口，但其实现/合并仍位于 Group C，依赖 W3 minimal spike 与 WA-06 TM Core，不是最初 source Qt import 的前置。
- 派发包必须附 W1/W3 reference、rooted/error/source authority delta 和禁止 Windows path-only proof 的反例。

### Group B — Persistence / Recovery
- WA-03 Project、WA-04 Resource、WA-05 TMX、WA-06 TM Core。
- 派发包必须附 publish mode/threat scope、candidate close/reopen、power-cut success boundary、W2 persistent receipt 与 recovery results。

### Group C — UI / Frozen Journey
- WA-07 Feature5/UI、WA-08 Qt increment；WR-01/02/03 仅进入 revalidation manifest。
- 派发包必须附 W3 bootstrap/loader contract、bundle-relative assets、qwindows/visible UI 与最终 clean-user journey。

## Merge Order and Parallelism

```text
W1 + Steering ownership
  -> Windows platform contracts/backends + POSIX parity
  -> [WA-01 Parser || WA-02 Chunk]

W3 -> minimal frozen spike
WA-01 -> [WA-03 Project || WA-04 Resource || WA-05 TMX]
[W2 + WA-01 + WA-02 + Windows backend] -> WA-06 TM Core
[W3 spike + WA-06] -> WA-07 Feature5/UI
[WA-03/04/05 + WA-07 + full packaging] -> WA-08 Qt increment
  -> WR-01/02/03 revalidation
  -> full onedir/windowed packaged E2E
  -> Windows + macOS/Linux final evidence
```

W3 packaging implementation可在 consumer amendments 期间继续，但 release closure 必须等待 WA-01～08 全部 merge；WA-05 owner 未获治理确认会阻塞 TMX cluster。任何平行线都不得从未合并工作树复制实现或用 patch-equivalent commit 规避 merge identity。

## Merge Ledger Schema

每个 `WA-*` 完成时追加一行实际事实；当前不预填虚假 commit/approval：

| Field | Required value |
|---|---|
| dispatch_id | 上表稳定 ID |
| owning_spec / branch | 人类批准的唯一 owner 与分支 |
| requirements_approval | approved revision/commit |
| design_approval | approved revision/commit，含 ADR mapping |
| tasks_approval | approved revision/commit，含 amendment task IDs |
| amendment_commit | 单一可追踪 commit；禁止只记工作树 hash |
| merged_into_windows | merge commit/parent，可达性验证 |
| evidence_manifest | portable artifact key + SHA-256；包含 R/D/T task suffix |
| disposition | `MERGED_PASS` / `BLOCKED` / `SUPERSEDED`，不得用 `SKIPPED` 放行 required row |

`WR-*` 记录 revalidation commit、artifact key、结果和“无 contract delta”的 owner确认。只有 ledger 全部闭合且实际代码差异仍落在获批边界内，最终 Windows Feature GO 才可进入审查。
