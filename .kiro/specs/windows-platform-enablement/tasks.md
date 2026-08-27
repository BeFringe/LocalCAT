# 实施计划

> **APPROVED FOR STAGED IMPLEMENTATION**：本计划按“现场失败/通过矩阵 → Windows 文件系统/锁适配 → consumer amendments → frozen-source/Windows packaging → clean EXE E2E”排序。ADR-020～023、`windows-platform-enablement` owning scope、WA-01～08 current R/D/T request、ledger与独立Design review均已闭合；从Task 1起仍必须逐项满足task/merge/evidence依赖，任何早期局部通过都不构成Windows Feature GO。owner 指 Spec/合同 authority；branch 只记录提交血缘，worktree/Agent/thread 都不是 owner。单个执行者或独立 reviewer 可以覆盖多个 Spec，但不能借此跨越各 Spec 审批门。实施/累计复审节奏见 `review-clustering.md`。

- [x] 0. 闭合 ADR、所有权、跨 Spec amendment 与设计授权

- [x] 0.1 由 Governance owner 完成人工审阅并采纳 ADR-020/W1、ADR-021/W2、ADR-022/W3 草案
  - W1 固定 live-handle identity、handle/share profiles、protocol-control lock exact ACL与可恢复首次初始化、`CREATE_IF_ABSENT`/`REPLACE_UNDER_LOCK`、retained readback→durable owner commit→terminal reproof、非 expected-ID CAS threat scope、稳定错误和版本化 `DurabilityProfile`
  - W2 初始固定 TokenUser/owner SID、`WindowsPrivateAclV1` exact DACL/AccessCheck、standard/elevated/local/domain/AzureAD matrix、FileId reuse反例及只作为业务envelope子记录的`WindowsPrivateProof`；Task 0.5随后发现MIC需由已采纳ADR-023以`WindowsPrivateSecurityV2`窄范围修订，历史采纳事实不被静默改写
  - W3 固定native entry在首次Python DLL/非KnownDLL load前的DLL policy、完整Boot TCB、`TrustedSourceAuthority`、retained-handle exact-byte `TrustedSourceLoader`、external `.py` closure、clean/content-addressed build provenance、onedir release gate；明确ADR-020依赖及ADR-011只拥有Feature 5/UI composition
  - 完成时，三个正式 ADR 编号、批准 revision 和取代/相交关系可从治理分支唯一追踪；任一未决即保持 NO-GO
  - _Requirements: 1.2, 2.1, 2.3, 3.4, 4.1, 4.2, 8.5, 8.6, 10.1, 10.6, 12.3_
  - _Boundary: Governance ADR Ownership_

- [x] 0.2 由 Governance owner批准 Windows owning scope 与 Steering 同步
  - 在治理分支把 `windows-platform-enablement` 记录到 `spec-ownership.md`/`roadmap.md`，固定本 Spec 只拥有共享合同/backends/bootstrap/build/release ledger，相邻 Spec 拥有 consumer business invariants
  - 确定 `tmx-context-interchange` 是唯一 owning Spec、`ui-mvp` 是其 amendment 提交血缘，并确认 Qt avatar 只作为 Windows 功能回归，不改变现有资源或打包边界
  - 完成时，scope lineage、Steering、Design 与 amendment ledger 无冲突，功能分支不产生重复 Steering 提交
  - _Requirements: 5.1, 10.5, 11.2, 11.4, 12.5_
  - _Boundary: Steering and Spec Ownership_
  - _Depends: 0.1_

- [x] 0.3a 派发 startup/source amendments
  - 按 `cross-spec-amendments.md` 向 WA-01 Parser、WA-02 Chunk、WA-07 CapabilityHost owners 派发精确 R/D/T delta；追加既有 task 的 `a` 后缀，不重排原任务。WA-07在本组只提前冻结接口，并与0.3c复用同一request revision、acknowledgement和ledger row，不制造第二次派发
  - 每个 owner 记录 approval/commit，覆盖 rooted source/writer、无顶层 `fcntl`、bootstrap-before-import、stable body-safe errors
  - 完成时，三项 dispatch 均有 owning Spec acknowledgement；lineage branch只记录后续提交/merge血缘，缺一项不得进入任务 4
  - _Requirements: 1.1, 1.2, 2.1, 2.6, 5.1, 5.2, 10.6_
  - _Boundary: Cross-Spec Dispatch Group A_
  - _Depends: 0.1, 0.2_

- [x] 0.3b 派发 persistence/recovery amendments
  - WA-03 Project、WA-04 Resource、WA-05 TMX 的原dispatch已获acknowledgement；WA-06因ADR-023把private profile从V1接管为V2，已以`R2` superseding request重新派发并获acknowledgement，不得静默改写已批准`R1`
  - 新request继续附 publish mode、owner lease、`DurabilityProfile`、owner-specific receipt + nested W2 proof和FileId reuse反例，并明确每项 R/D/T suffix、merge dependency、zero-mutation/recovery evidence；WA-05保持任务0.2批准的唯一owner
  - 完成时，WA-03/04/05与当前WA-06 revision均有owning Spec acknowledgement；`R1`只保留为`SUPERSEDED`历史，lineage branch只记录后续提交/merge血缘，缺一项不得进入任务5/6
  - _Requirements: 3.1, 4.1, 4.3, 5.1, 7.1, 8.1, 8.4, 9.1_
  - _Boundary: Cross-Spec Dispatch Group B_
  - _Depends: 0.1, 0.2, 0.4a_

- [x] 0.3c 派发 UI/frozen journey amendments与 revalidation-only manifest
  - 完成0.3a同一WA-07 Feature5/UI request并派发 WA-08 Qt increment；将 WR-01 TM store、WR-02 termbase、WR-03 old Qt 标为 revalidation-only/no-amendment，不制造重复dispatch或空洞任务
  - 固定 qwindows/visible window、avatar catalog解码与无匹配头像fallback、clean-user/non-repository CWD 与 source-proof failure diagnostics
  - 完成时，UI/frozen dispatch 与 revalidation理由均获 owner确认；缺少 WA-07/08 merge 不得进入最终 EXE gate
  - _Requirements: 6.2, 6.4, 6.5, 10.1, 10.5, 11.2, 11.3, 12.1_
  - _Boundary: Cross-Spec Dispatch Group C_
  - _Depends: 0.1, 0.2_

- [x] 0.4 冻结 amendment merge ledger 与实现依赖图
  - ledger schema、WA-01～08唯一current dispatch request revisions、owning Spec acknowledgement与lineage branch已批准；WA-06 `R1`已登记为`SUPERSEDED`且禁止驱动实现/merge/evidence，`R2`已登记并复核为唯一current request。本任务不预填尚未发生的 amendment/merge commit、artifact 或终态 disposition
  - 验证合并顺序为 platform → Parser/Chunk → Project/Resource/TMX → TM Core → Feature5/UI → Qt journey；平行线不得复制 patch-equivalent commit
  - 完成时，ledger schema/dispatch identity获批且每个实现 cluster都有fail-closed dependency gate；实际 commit/merge/evidence/disposition由任务4.1/5.1/6.1/6.5/7.5逐行追加，禁止用`SKIPPED`放行required row
  - _Requirements: 5.1, 5.3, 12.3, 12.4, 12.5_
  - _Boundary: Amendment Merge Authority Ledger_
  - _Depends: 0.3a, 0.3b, 0.3c_

- [x] 0.4a 由Governance owner审阅并采纳ADR-023 pre-authority proof-order补充决策
  - 审阅Task 0.5发现的四个跨既有ADR执行缺口：`ERROR_FILE_EXISTS`后的LOCK-first初始化分流、W1/W2 exact medium mandatory-integrity表示、W1 durability registry到W3 bundle authority、pre-authority dynamic native roots的manifest/pre-load proof
  - ADR-023必须区分补充与取代：LOCK-first、`PendingPublication`、durability registry和dynamic native closure只细化既有owner/authority；仅V1 DACL-only security profile由V2窄范围取代。已采纳ADR可更新状态、同步结果和不改变语义的勘误，但不得把后来发现的长期约束静默写成初始决策；不新增evidence审批门
  - 完成时，ADR状态/README/补充与精确profile取代关系与本Spec Governance Impact一致；当前已获人工批准
  - _Requirements: 3.6, 4.2, 8.6, 10.6, 11.2_
  - _Boundary: Supplemental Windows Pre-Authority ADR_
  - _Depends: 0.1, 0.2_

- [x] 0.5 完成独立对抗性设计评审并取得人工 Design approval
  - 由未参与实现上下文的Cumulative reviewer重放native-entry/Python-DLL时序与bootstrap TCB/chicken-and-egg、lock首次初始化/creator crash、candidate self-sharing、readback→commit窗口、pre-snapshot non-CAS、真实断电profile映射、FileId reuse、exact ACL/token与source/bytecode duplicate反例
  - 所有 blocker 必须在 Requirements/Design/ADR/ledger 中得到可执行处置；只写 Implementation Notes 或说明“实现时再看”不构成关闭
  - 完成时，review disposition、剩余风险、Design approval 和 `spec.json` 状态一致，才允许任务 1及以后开始
  - _Requirements: 1.2, 2.3, 4.2, 8.5, 10.3, 12.2, 12.3_
  - _Boundary: Independent Adversarial Design Gate_
  - _Depends: 0.4, 0.4a_

- [x] 1S. 建立可移植 baseline、日志模型与 stock frozen 路线裁决

- [x] 1.1 在精确基线重放 Windows 失败/通过矩阵
  - fail-closed 核对 `ui-mvp@b925b803d81001f55dea46ace8b26159ca82db19`、Windows 11、CPython 3.14 x64、clean venv 和无用户 WIP覆盖
  - 重放 requirements/pip check、Qt visible window/qwindows、SQLite FTS5/trigram、source startup `fcntl`、Parser rooted、Project/TM/TMX 与 baseline PyInstaller onedir/windowed
  - 完成时，每项有 exact PowerShell command、版本、exit code、stdout/stderr、SHA-256 inventory 和 PASS/FAIL reason；历史报告只能交叉核对，不能替代 fresh evidence
  - _Requirements: 6.1, 6.2, 7.1, 8.1, 9.1, 9.3, 11.1, 12.4_
  - _Boundary: Fresh Baseline Evidence_
  - _Depends: 0.5_

- [x] 1.2 实现 portable release evidence schema 与无泄漏日志 harness
  - 输出到仓库相对 `artifacts/windows/<commit>/<run-id>/` 或批准的 CI artifact key；manifest记录 commands、environment、exit、log digest、matrix/checklists，不以本机绝对路径充当 identity
  - scenario oracle来自本Spec拥有的版本化`packaging/windows/evidence-scenarios/<lane>.json`；harness/validator只接受可信repository root + 该目录lane key并绑定合同摘要，release validator还要求clean tracked orchestrator提供expected commit/contract digest外部锚。CWD role由该root推导，system PowerShell不经ambient `PATH`选择，effective environment由受版本控制profile完整构造且每条command的exact non-secret projection进入oracle；optional/incomplete scenario不得掩盖实际containment failure或interruption
  - 文件系统事件记录 operation/profile、volume/filesystem、live FileId comparison、final-path/reparse verdict、share flags、LockFileEx range、flush/rename/close/reopen/recovery phase与稳定 code；不得记录正文/secret/raw private path
  - 受版本控制的`evidence-harness-success.json`、`evidence-harness-expected-failure.json`与`evidence-harness-interrupted.json`分别固定成功、预期失败和中断lane；producer `run_windows_evidence_harness_smoke.py`只声明bundle内部一致性，clean tracked `anchor_windows_evidence_harness_smoke.py`另从HEAD与`git show`取得commit/contract外锚、独立计算生成后的manifest摘要并重验。Task 1原始build/dist与含本机路径的采集日志只保留在content-addressed本地/CI artifact，不进入Git提交
  - 完成时，真实成功/失败/中断日志均可由 schema validator复核，windowed failure 有 marker/diagnostic而非静默退出
  - _Requirements: 1.2, 6.4, 12.3, 12.4_
  - _Boundary: Windows Evidence and Diagnostic Contract_
  - _Depends: 1.1_

- [x] 1.3 (P) 固定低密度 Windows 场景与迁移清单
  - 扫描 production/tests 中 `fcntl/flock/dir_fd/O_DIRECTORY/O_NOFOLLOW/directory fsync/st_dev/st_ino` 与 CWD/checkout resource path，按 owning Spec 和 amendment ID分类
  - 扩充 target-open、self-sharing、kill release、junction/reparse/hardlink/ancestor swap、FileId reuse、ACL token、power-cut与 frozen visibility 场景清单
  - 完成时，清单能追踪到具体 consumer、合同/反例、owner、task 和 evidence key；不得把静态无命中当 runtime pass
  - _Requirements: 2.2, 2.3, 3.2, 4.4, 5.2, 10.2, 11.5, 12.2, 12.4_
  - _Boundary: Windows Scenario Inventory_
  - _Depends: 1.1_

- [x] 1.4 审计stock PyInstaller frozen入口并触发W3 reapproval
  - 使用锁定的CPython 3.14.x、PyInstaller 6.22.x官方sdist、stock `runw.exe`与Task 1.1 baseline EXE，逐项审计native entry、PE imports/delay-load、DLL search、native closure、module reproof、attestation handoff与pre-authority hook时序
  - 六项mandatory断言全部FAIL时只完成“stock route NO-GO”事实：不得构建source-only旁路、复制文件、runtime hook追认或把普通onedir标为release；完整W3 feasibility仍由1.6负责
  - 完成时，pinned source/binary/evidence关系、机器可读矩阵、完整日志和NO-GO报告可独立重放；结果触发1.5 reapproval并阻断所有frozen consumer与任务7，但不阻断任务2～6的source阶段
  - _Requirements: 10.1, 10.3, 10.6, 11.1, 12.3_
  - _Boundary: W3 Stock Entry NO-GO Gate_
  - _Depends: 1.2_

- [ ] 1F. 规划并验证W3 custom in-process entry可行性

- [ ] 1.5 重新批准W3 custom in-process entry计划
  - 固定同一进程真实entry、本机或CI受支持MSVC x64/Windows SDK的candidate选择与构建前candidate-input lock、PyInstaller source pin与patch owner/apply合同、expected PE/system allowlist、manifest parser/hash最小TCB、retained-handle→restricted load→actual-module reproof、native→Python handoff ABI、exact-source loader import order和升级维护边界
  - 审批输入为`w3-custom-entry-plan.md`：首选release-owned PyInstaller 6.22.2 `runw`下游patch、E0～E11 entry顺序、领域语义命名的one-shot opaque extension ABI、无自引用的pre-link/runtime/post-build manifests、MSVC x64 candidate lock schema及14项mandatory spike矩阵；W3只保留为治理/证据ID，不进入产品runtime ABI命名
  - 明确轻量source launcher与父wrapper均不属于W3 boot entry；保持ADR-022唯一bootstrap authority、onedir profile、source proof和全部fail-closed要求，不用本任务新增第二发行权威
  - 完成时，由本机或CI在candidate构建前materialize的compiler/SDK/tool与upstream/runtime摘要、patch owner/apply合同、exact Python C API目标表、pre-authority TCB边界及expected native/PE/system allowlist均进入candidate-input lock，并连同Design/Tasks/攻击矩阵获W3 reapproval；本任务可与source lane并行，1.6从该批准状态开始
  - _Requirements: 10.1, 10.3, 10.6, 11.1, 12.3_
  - _Boundary: W3 Custom Entry Reapproval_
  - _Depends: 1.4_

- [ ] 1.6 在W1 rooted contract冻结后完成custom entry最小W3 spike
  - 仅构建已批准完整Boot TCB、一个source-only critical module和一个fixture；在首次Python DLL/非KnownDLL load前闭合搜索、递归native closure与retained-handle proof，load后复核actual module identity并完成不可伪造handoff
  - `TrustedSourceLoader`只从retained verified handle读取manifest匹配的exact bytes并直接编译执行，attestation/digest与metadata一致且无`.pyc`/`__pycache__`/PYZ duplicate；覆盖未声明dynamic load、顶层/传递DLL注入、非仓库CWD、reparse/swap/manifest/source tamper
  - 生成applied patch/source digest、resulting PE、realized Python C API绑定表、实际native dependency inventory与PE/system allowlist并写入candidate-input digest绑定的realized-build lock；任一结果偏离1.5合同即回到W3
  - 完成时全部mandatory断言PASS并保存content-addressed toolchain/dist inventory；任一失败继续保持W3、frozen WA与Task 7 NO-GO，不影响source milestone
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.1, 12.3_
  - _Boundary: W3 Custom Minimal Frozen Feasibility Gate_
  - _Depends: 1.5, 2.1, 3.2_

- [ ] 2. 建立共享平台合同与 POSIX parity adapter

- [ ] 2.1 定义 backend-neutral authority、identity、lock、publish 与 private proof 合同
  - 实现context-managed opaque authorities、live-only`FileObjectIdentity`、opaque private-proof port、`PublishMode`/`PublishFacts`、跨owner durable commit保持retained destination的`PendingPublication`与stable`PlatformFileError`；`begin_publish`不得返回success，平台合同不得把business phase/generation/compatibility合并成generic receipt
  - 禁止上层取得 raw HANDLE/dirfd，禁止 platform API宣称 expected-target CAS，关闭后 authority 不可复用
  - 完成时，shape/type/name/error/closed-handle与非法组合合同 tests 全绿
  - _Requirements: 1.2, 2.1, 3.3, 3.5, 4.1, 5.1, 5.3_
  - _Boundary: Platform File Contracts_
  - _Depends: 1.2, 1.3_

- [ ] 2.2 固定现有 POSIX 行为的 characterization matrix
  - 在迁移前记录 Parser/Chunk/Project/Resource/TMX/TM 的 success/error/receipt/recovery bytes与 macOS/Linux支持条件
  - 覆盖 openat/dirfd/O_NOFOLLOW/flock/file+directory fsync、candidate/readback/LKG 和 direct-import boundaries
  - 完成时，矩阵能区分 business invariant 与 POSIX implementation fact，后续 adapter parity 有唯一 oracle
  - _Requirements: 1.4, 4.3, 5.3, 5.4, 12.5_
  - _Boundary: POSIX Characterization Baseline_
  - _Depends: 2.1_

- [ ] 2.3 把 POSIX primitives 迁入唯一 adapter并保持语义
  - 将 `fcntl`、dirfd/openat/no-follow、pread、file/directory fsync 封装进 `platform_fs_posix.py`；纯合同/composition模块在 Windows import 时不加载 POSIX module
  - 保持已固定 public codes、receipt/recovery和 fail-closed behavior，不借重构改变业务 authority
  - 完成时，共享 contract tests 与任务 2.2 characterization 全绿，Mac/Linux consumer diff可追踪到相邻 amendments
  - _Requirements: 1.1, 1.4, 5.1, 5.2, 5.3, 5.4_
  - _Boundary: POSIX Platform Adapter_
  - _Depends: 2.2_

- [ ] 2.4 实现 fail-closed composition factory 与 backend self-probe
  - factory只在 OS/volume/backend contract gate通过后返回 capability；Windows不得导入 POSIX adapter，POSIX不得导入 Win32 API
  - 未知平台、unsupported volume、structure/API/self-probe失败映射稳定 capability error，不以布尔常量或 fallback path I/O开放能力
  - 完成时，import graph/AST、factory matrix和 fault seam均证明没有隐藏 authority
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 5.2, 5.4_
  - _Boundary: Platform Composition Factory_
  - _Depends: 2.3_

- [ ] 3. 实现 Windows rooted/lock/private/publish/durability backend

- [ ] 3.1 实现 Win32 FFI wrapper、RAII handle 与受支持主机 Gate
  - 精确绑定CreateFileW、GetFileInformationByHandleEx、GetFinalPathNameByHandleW、SetFileInformationByHandle、FlushFileBuffers、LockFileEx/UnlockFileEx、GetSecurityInfo/AccessCheck、storage write-cache query及结构/错误
  - 首版只mint匹配批准`DurabilityProfile`的Windows 11 x64 local fixed NTFS；UNC/remote/ReFS/FAT/exFAT、unknown cache/flush/write-through/power-protection facts或probe缺失均`CAPABILITY_UNAVAILABLE`
  - 完成时，结构尺寸/argtypes/restype/last-error/close-on-failure、自检和 unsupported matrix 全绿
  - _Requirements: 1.2, 2.1, 2.2, 3.1, 4.2_
  - _Boundary: Windows Native API and Host Gate_
  - _Depends: 2.4_

- [ ] 3.2 实现逐组件 rooted handle authority 与 sealed handle-read
  - 以 ROOT/INTERMEDIATE/SOURCE profiles保留 ancestor/final handles，拒绝 reparse/nonregular/ADS/device/reserved/ambiguous names，比较 final path/volume/live FileId
  - content与fixture只从已证明final handle读取；路径检查失败前不读body，创建时重新证明bound parent
  - 完成时，junction/symlink/mount/final+ancestor swap/hardlink policy/case/trailing-dot matrix获得稳定结果且无逃逸
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_
  - _Boundary: Windows Rooted Authority_
  - _Depends: 3.1_

- [ ] 3.3 (P) 实现 LockFileEx persistent lease 与 crash release
  - lock file使用W1 exact protocol-control DACL、exact medium mandatory-label/`NO_WRITE_UP` SACL projection、deterministic name、single-link/`ProtocolControlLockPayloadV1`/entry identity，文件永久保留且不unlink/replace；不得反向依赖W2 attestation private proof
  - 首次creator用`CREATE_NEW`+share-none init handle写完整可重算payload并flush/readback；并发loser在`ERROR_FILE_EXISTS`后先用普通LOCK profile open/复证，完整payload直接进入`LockFileEx`，该open sharing violation才bounded retry INIT。空/strict-prefix须关闭普通handle再争抢INIT share-none handle、二次复证DACL/MIC/identity/bytes后确定性恢复；unknown状态fail closed，恢复后关闭INIT再打开普通LOCK profile
  - 实现明确 byte range、blocking/timeout/contention结果；normal unlock/close与 TerminateProcess 后 eventual release不承诺公平/零延迟
  - 完成时，双进程acquire/timeout、two-creator、create/write/flush/readback/close逐边界crash接管、payload/FileId tamper、kill/retry与错误映射全绿
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 8.3_
  - _Boundary: Windows Process File Lock_
  - _Depends: 3.2_

- [ ] 3.4 (P) 实现 W2 private storage 与跨重启 proof重新证明
  - 按ADR-021经ADR-023接管后的`WindowsPrivateSecurityV2`，对private dir/device key/attestation/candidate显式创建owner=TokenUser SID、DACL present/protected且非defaulted/auto-inherited，exact三条AceFlags=0的TokenUser/`S-1-5-18`/`S-1-5-32-544` `ACCESS_ALLOWED_ACE(FILE_ALL_ACCESS)`，并显式设置/重验单一medium `SYSTEM_MANDATORY_LABEL_ACE`+`NO_WRITE_UP` projection；在`READ_CONTROL` handle上以`LABEL_SECURITY_INFORMATION`读取label，不请求完整`SACL_SECURITY_INFORMATION`/`ACCESS_SYSTEM_SECURITY`。audit ACE分开解析，拒绝额外/漂移mandatory label及未知授权ACE/principal，V1/unknown profile不兼容读取
  - `AccessCheck`使用当前primary token复制的`SecurityImpersonation` token；`GENERIC_ALL`经file/directory mapping后必须精确为`FILE_ALL_ACCESS`且全部获准，真实同SID low-integrity/restricted子进程的open/write/delete负向矩阵必须被OS拒绝；owner先摘要不含proof的canonical private-context，W2再对proof unsigned projection做domain-separated HMAC，输出`WindowsPrivateProof`，其`security_profile_id`与authority descriptor digest绑定同一V2 canonical owner+DACL+MIC projection，再由Gate D/canonical owner分别嵌入自身envelope
  - 完成时，standard/elevated/local/domain/AzureAD正向矩阵通过，low-integrity/restricted/service/AppContainer/impersonation与未知 ACE/owner/tamper fail closed
  - _Requirements: 1.2, 8.4, 8.5, 8.6_
  - _Boundary: Windows Private Storage and Persistent Re-attestation_
  - _Depends: 3.2_

- [ ] 3.5 实现 handle-bound publish 的两个模式与精确生命周期
  - candidate `CREATE_NEW`/share=none，完成 write+FlushFileBuffers；支持 create-if-absent与持有resource-family lease的replace-under-lock
  - 由`begin_publish`返回opaque `PendingPublication`，严格执行rename→capture final facts→close every candidate handle→reopen/readback并保留no-write/no-delete destination handle→owner durable commit与business-state reproof→platform terminal identity/digest reproof→close；preliminary facts不得当success，pre-snapshot只作stale/recovery fact，不声称CAS
  - 完成时，同进程 close前reopen得到预期 sharing violation，close后成功；协作writer被lease排除，不合作instruction-boundary swap只产生检测失败/recovery-required
  - _Requirements: 3.4, 4.1, 4.3, 4.4, 4.5_
  - _Boundary: Windows Bound Publisher_
  - _Depends: 3.2, 3.3_

- [ ] 3.6 在真实 reboot 边界批准 NTFS durability success contract
  - 生成W1 owner发布、clean tracked、版本化canonical `packaging/windows/durability_profiles.json`，把Windows build、NTFS/volume、storage bus/controller、write-cache、write-through、flush与power-protection facts绑定到disposable VM/VHD或批准物理lab的portable forced-power-off/reboot evidence manifest digest
  - runtime必须精确匹配该profile且每次重启只接受完整old、完整new或owner recovery-only；只匹配filesystem name、unknown/changed cache facts或无法映射evidence family时，arm前返回`DURABILITY_UNAVAILABLE`，arm后不确定返回`RECOVERY_REQUIRED`
  - 完成时，W1定义与实际matrix一致且可复现；process kill/fault injection仅作为补充，不能替代此门
  - _Requirements: 4.2, 4.3, 4.5, 7.4, 8.4, 12.2, 12.3_
  - _Boundary: Windows NTFS Power-Cut Durability Gate_
  - _Depends: 3.5_

- [ ] 3.7 运行共享合同与完整 Windows 对抗性矩阵
  - 覆盖 rooted、share、lock、private、publish、identity reuse、target-open、kill、fault/power和resource cleanup，mandatory case不得 skip
  - 由Cumulative reviewer核对日志事实与稳定code，特别验证没有path-only proof、expected-CAS宣称、share扩大、FileId永久化或W1/W2依赖循环
  - 完成时，Windows adapter可由factory mint；任何 mandatory失败保持 capability unavailable
  - _Requirements: 1.2, 2.2, 2.3, 3.1, 4.3, 4.4, 5.4, 12.2, 12.3_
  - _Boundary: Windows Platform Capability Gate_
  - _Depends: 3.4, 3.6_

- [ ] 4. 合并 Parser/Chunk amendments并恢复 source Qt启动

- [ ] 4.1 验证并合并 WA-01 Parser 与 WA-02 Chunk amendment commits
  - 核对 owning Spec acknowledgement、lineage branch、R/D/T approvals、task suffix、commit/merge parent和portable evidence，不从其他工作树复制patch
  - 更新ledger为 `SOURCE_MERGED_PASS`前重跑双方contract/adversarial tests；任一身份不一致停止，frozen阶段未闭合前不得写入terminal `MERGED_PASS`
  - 完成时，Windows分支可达两个approved merge，且业务错误/状态机仍由原 owner拥有
  - _Requirements: 2.6, 3.1, 5.1, 5.3, 12.5_
  - _Boundary: Parser and Chunk Amendment Merge_
  - _Depends: 0.3a, 3.7_

- [ ] 4.2 验证 Parser Windows source/writer vertical slice
  - 真实读写获授权source，执行sealed copy、codec/body-safe/stale/atomic writer/readback；所有原POSIX-only rooted adversarial cases在Windows backend运行
  - 能力不可用仍返回既有`PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE`/获批子码，且目标/正文零变更
  - 完成时，Windows source Parser通过且形成可供Project/TMX/resource消费的正式owner port，无mock/skip/现场patch；frozen复验由WA-01 5.12b与Task 7负责
  - _Requirements: 1.2, 2.1, 2.2, 2.6, 5.3, 10.3_
  - _Boundary: Parser Windows Vertical Slice_
  - _Depends: 4.1_

- [ ] 4.3 验证 collaborative chunk lock/publish/recovery vertical slice
  - 证明import/controller composition不再顶层加载`fcntl`，chunk publish沿LockFileEx+publisher并保留journal/LKG/recovery codes
  - 运行双进程竞争、target-open、kill release、rename/close/readback faults与restart recovery
  - 完成时，唯一authority/old-new-recovery矩阵全绿且无direct POSIX primitive
  - _Requirements: 1.1, 3.1, 3.2, 4.3, 5.2, 6.3_
  - _Boundary: Collaborative Chunk Windows Vertical Slice_
  - _Depends: 4.1_

- [ ] 4.4 在源码环境启动真实 LocalCAT Qt主窗口
  - clean CPython 3.14 x64 venv安装`requirements-ui.txt`并`pip check`；不得安装xlwings/Excel或注入兼容patch
  - 使用Qt windows plugin加载真实`qwindows.dll`、创建可见主窗口、进入事件循环并输出受控smoke marker
  - 完成时，source LocalCAT从非仓库CWD启动，不因POSIX import/capability composition退出；缺插件路径返回可诊断失败
  - _Requirements: 1.1, 1.5, 6.1, 6.2, 6.3, 6.4_
  - _Boundary: Windows Source Qt Startup_
  - _Depends: 4.2, 4.3_

- [ ] 5. 合并 Project/Resource/TMX amendments并完成持久化 vertical slices

- [ ] 5.1 验证并合并 WA-03 Project、WA-04 Resource、WA-05 TMX commits
  - 核对owner/approvals/task suffix/commit/merge/evidence；WA-05 owner必须与任务0.2一致
  - 合并后重跑共享rooted/publish contract和各owner业务baseline，ledger仅在fresh pass后标`SOURCE_MERGED_PASS`；frozen阶段未闭合前不得写入terminal `MERGED_PASS`
  - 完成时，三个consumer都通过platform port且无重复Windows filesystem实现
  - _Requirements: 5.1, 5.2, 5.3, 7.1, 9.1, 12.5_
  - _Boundary: Persistence Amendment Merge_
  - _Depends: 0.3b, 4.4_

- [ ] 5.2 完成Project保存、退出、重开与deterministic carrier验证
  - 保存真实项目并重新打开相同protected content/metadata；跨进程重启重新证明authority，不复用内存身份
  - 覆盖junction/ancestor swap/target-open/双进程竞争、deterministic ZIP members、LKG和fault/reboot recovery
  - 完成时，canonical bytes/digest与old/new/recovery结果满足ADR-018/019且无部分覆盖
  - _Requirements: 4.3, 7.1, 7.2, 7.3, 7.4_
  - _Boundary: Windows Project Save and Reopen_
  - _Depends: 5.1_

- [ ] 5.3 (P) 完成Resource/Termbase portability与receipt验证
  - 验证resource artifact/package/importer/repository/ledger/workspace state与termbase consumer通过shared ports保存/重开
  - hostile source、target-open、receipt tamper和resource-local failure不影响其他authority；WR-02 column-selection regression保持业务语义
  - 完成时，resource/termbase结果、bytes与portable receipts可重启复核
  - _Requirements: 2.2, 4.3, 5.1, 5.3, 7.3_
  - _Boundary: Windows Resource Portability_
  - _Depends: 5.1_

- [ ] 5.4 (P) 完成TMX获授权导入、canonical保存与重启验证
  - 从真实rooted source导入有效TMX，保持locale normalization/conflict rules/准确count并保存canonical target
  - escaped/reparse/swap/invalid source在发布前fail closed且目标byte hash不变；进程重启后结果仍可查询
  - 完成时，source TMX journey全绿并交付frozen阶段可复用的owner port，无import count伪成功；packaged复验由WA-05 5.4a与Task 7负责
  - _Requirements: 2.2, 9.1, 9.2, 9.4_
  - _Boundary: Windows TMX Import and Persistence_
  - _Depends: 5.1_

- [ ] 5.5 运行Project/Resource/TMX组合并发、崩溃与power-cut矩阵
  - 在owner resource lease下重放save/import/export faults、uncooperative target swap、opened-target rejection和reboot recovery
  - 验证每个business owner只消费platform facts并决定journal/LKG，不将pre-snapshot当CAS或擅自清理ambiguous residue
  - 完成时，三个vertical slices只产生完整old/new或recovery-only且日志可审计
  - _Requirements: 3.4, 4.2, 4.3, 4.5, 7.3, 7.4, 9.2, 12.2_
  - _Boundary: Persistence Recovery Matrix_
  - _Depends: 5.2, 5.3, 5.4_

- [ ] 6. 合并TM Core/Feature5 UI amendments并完成activation/FTS5恢复

- [ ] 6.1 验证并合并 WA-06 TM Core amendment
  - 核对W1/W2及ADR-023 superseding映射、当前approved R/D/T request revision与task suffix、feature5 commit/merge identity和activation/attestation/recovery evidence；`R1`或V1实现不得进入merge
  - 重跑Core frozen contracts、canonical migration/retrieval、FileId reuse、ACL token、two-process/power-cut和WR-01 module-extraction regression
  - 完成时，Core business authority仍归`tm-storage-retrieval-index`，Windows分支只提供platform能力和source merge evidence；ledger记录`SOURCE_MERGED_PASS`，frozen复验后才可进入terminal状态
  - _Requirements: 5.1, 8.1, 8.3, 8.4, 8.5, 8.6, 12.5_
  - _Boundary: TM Core Amendment Merge_
  - _Depends: 0.3b, 3.7, 5.5_

- [ ] 6.2 完成TM首次激活、双进程唯一authority与restart recovery
  - 激活合法source，序列化bootstrap/reservation，发布唯一canonical SQLite generation并打开精确published store
  - 覆盖双进程首次激活、TerminateProcess、reservation/migration/seal/publish/cleanup faults与重启；失败方返回稳定竞争/既有authority结果
  - 完成时，无重复generation/部分authority，published-tail恢复同一generation，unknown residue保持unavailable
  - _Requirements: 3.1, 3.2, 8.1, 8.2, 8.3, 8.4_
  - _Boundary: Windows TM Activation and Recovery_
  - _Depends: 6.1_

- [ ] 6.3 验证W2 attestation、FileId reuse与device-local恢复
  - restart时由Gate D/canonical owner分别重验自身compatibility或generation/phase envelope、exact bytes/digest及nested `WindowsPrivateProof`/device-secret MAC；历史Volume/FileId只作反例/diagnostic
  - 覆盖delete/recreate复用、owner/ACE/inheritance/link/reparse/volume/tamper、standard/elevated/domain/AzureAD token；不可证明则fail closed且不回落legacy
  - 完成时，合法authority恢复，任何弱路径/token/identity替代均被拒绝
  - _Requirements: 8.2, 8.4, 8.5, 8.6_
  - _Boundary: Windows TM Attestation Re-proof_
  - _Depends: 6.2_

- [ ] 6.4 完成SQLite FTS5/trigram真实创建、查询、关闭与重开
  - 在Windows source runtime对canonical store创建实际FTS5/trigram schema，写入数据、执行MATCH/排序，关闭进程后重开同一published authority查询
  - 不只检查compile options；运行时缺能力返回稳定unavailable并阻止依赖发布/激活，不切换未批准算法
  - 完成时，结果与Core排序合同一致并包含exact version/options/schema/query evidence
  - _Requirements: 9.3, 9.4, 9.5_
  - _Boundary: Windows SQLite FTS5 Runtime_
  - _Depends: 6.2_

- [ ] 6.5 验证并合并 WA-07 Feature5/UI source阶段
  - 核对Feature5/UI owner route tasks、commit/merge identity；source composition先由Windows platform factory建立rooted source、lock与private-proof ports，再进入CapabilityHost/Controller，不消费ADR-022 `TrustedSourceAuthority`
  - 运行activation/restart projections、resource-local safe state、source bootstrap-before-business-import、no raw proof/path UI和existing Gate A/C/D contract regression
  - 完成时，source UI可激活TM、重启恢复并查询FTS5；ledger只记录`SOURCE_MERGED_PASS`，不得提前记录terminal `MERGED_PASS`
  - _Requirements: 5.1, 6.3, 8.1, 8.2, 9.4_
  - _Boundary: Feature5 UI Windows Source Integration Merge_
  - _Depends: 0.3a, 0.3c, 6.3, 6.4_

- [ ] 6.6a 实现并验证user-managed Windows轻量GUI入口
  - 用户安装CPython 3.14 x64、创建专用venv并按`requirements-ui.txt`安装依赖；入口只使用受验证的绝对`pythonw.exe`和source bootstrap，不复制Python/PySide6/Qt/source或冒充packaged release
  - 从non-repository CWD启动真实可见窗口并验证qwindows、图标/版本、环境清理、single-instance/退出与环境/source路径失效诊断；本任务只交付可被WA-08使用的入口，不提前声称完整产品journey
  - 完成时WA-08 5.4a可经该入口运行，且入口没有取得ADR-022 bootstrap/packaging authority
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 8.1, 9.1, 9.3, 9.4_
  - _Boundary: Windows User-managed Source Launcher_
  - _Depends: 4.4, 6.5_

- [ ] 6.6b 汇总user-managed Windows source产品journey
  - 只接受WA-08 5.4a经6.6a轻量入口产生的fresh evidence，覆盖项目保存/重开、TM激活/退出/重启恢复、TMX导入、FTS5查询、qwindows、资源与头像fallback；同时复核5.5与6.5的owner/recovery证据仍绑定同一commit
  - 完成时记录`WINDOWS_USER_MANAGED_RUNTIME_VERIFIED`与各WA row的`SOURCE_MERGED_PASS`；Requirement 10～12、W3与最终Windows状态继续NO-GO/NOT_VERIFIED
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 8.1, 9.1, 9.3, 9.4_
  - _Boundary: Windows User-managed Source Runtime Milestone_
  - _Depends: 5.5, 6.5, qt-editor-json-mvp-increment 5.4a_

- [ ] 7. 构建完整 frozen-source closure 与 Windows onedir/windowed发行物

- [ ] 7.0 合并frozen pre-build消费合同与WA-07 trusted bootstrap实现
  - 在1.6全PASS后，只合并生成manifest/build前必须存在的owner roots/hooks与WA-07 3.6a `TrustedSourceAuthority`消费实现；WA-01/02/04/05/06及WA-07其余packaged revalidation不得在发行候选生成前标为完成
  - CapabilityHost只消费native entry移交的`TrustedSourceAuthority`，完整Boot TCB先于platform factory与业务import；source阶段结果与最小spike都不得代答full candidate runtime
  - 完成时ledger只追加`FROZEN_PREBUILD_COMMITTED`事实，所有owner row仍等待7.4a post-build revalidation，terminal `MERGED_PASS`继续禁止
  - _Requirements: 5.1, 10.1, 10.2, 10.3, 10.5, 10.6, 12.5_
  - _Boundary: Frozen Consumer Amendment Merge_
  - _Depends: 1.6, 6.6b, feature5-ui-integration 3.6a_

- [ ] 7.1 生成deterministic frozen source/fixture/data manifest
  - 从approved owner roots解析Gate A/C/D、benchmark contract与dynamic import递归闭包，记录schema/commit/reason/relative path/kind/SHA-256和UTF-8排序
  - production build要求clean tracked tree；spec/hooks/generator/bootstrap/owner roots、W1 `durability_profiles.json`及其portable power-evidence manifest digests、manifest-declared dynamic native roots、locked wheels、bootloader/Python/native runtime全部content-addressed。critical modules只允许source-only collection，无`.pyc`/`__pycache__`/未声明PYZ duplicate
  - 完成时，manifest generator可重复产生byte-identical output并覆盖真实`.py`、JSON/TXT fixtures及获批tm/terms/logo/benchmark/profile assets
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 11.2, 11.5_
  - _Boundary: Frozen Source Manifest_
  - _Depends: 7.0_

- [ ] 7.2 实现native boot entry到CapabilityHost的可信handoff
  - release-owned native bootloader审计entry前PE imports/delay-load，在首次Python DLL/非KnownDLL load前固定搜索、拒绝CWD/PATH、绑定bundle/native目录，递归枚举native static/delay-load closure与manifest-declared dynamic native roots并对每个非系统DLL完成pre-load retained-handle/root/reparse/live-id/digest proof；未声明dynamic load fail closed，随后才加载、复核actual module identity并移交完整bundle/DLL attestation
  - `TrustedSourceLoader`从retained handle读取、摘要并直接编译exact `.py` bytes后mint不可序列化`TrustedSourceAuthority`；同一bundle authority向platform factory提供manifest-bound durability profile registry handle，外置CapabilityHost验证loader attestation/source digest/origin/co_filename/Gate closure，不以metadata相等代答executed bytes
  - 完成时，bootstrap不导入待验证adapter但满足W1 invariants，Boot TCB/source/fixture handle-read、bytecode/PYZ、tamper/reparse/DLL injection/circular-trust tests全绿
  - _Requirements: 1.2, 10.1, 10.2, 10.3, 10.5, 10.6_
  - _Boundary: Native Boot and Frozen Trust Handoff_
  - _Depends: 7.1_

- [ ] 7.3 建立受版本控制的Windows PyInstaller build definition
  - 使用`--onedir --windowed`、独立locked build requirements、custom spec/hook与必要的release-owned/customized native bootloader、source-only module map；clean tracked input与所有实际build inputs/dists均有digest，不把PyInstaller/xlwings加入UI runtime requirements
  - 收集Qt实际plugins与`platforms/qwindows.dll`、`.ico`、version metadata和mandatory data；不从build checkout绝对路径读取runtime依赖
  - 完成时，clean build venv可由单组PowerShell命令重建相同layout，build exit 0不替代runtime gates
  - _Requirements: 1.5, 6.1, 6.4, 11.1, 11.2, 11.4_
  - _Boundary: Windows PyInstaller Build_
  - _Depends: 7.2_

- [ ] 7.4 验证dist可见性、资源fallback与repository-path absence
  - inventory验证qwindows、critical `.py`、fixtures、tm/terms/logo/benchmark、W1 durability profile registry及portable evidence digest、ico/version和实际Qt plugins；assert retained-handle loader attestation、exact executed-source digest与无`.pyc`/PYZ duplicate
  - manifest声明avatar catalog时验证真实Qt索引/解码匹配；未声明catalog或无匹配头像时验证“— / 无内置头像”fallback。从non-repository CWD/clean environment启动且不访问source checkout
  - 完成时，missing/extra/tamper/checkout-access均令release validator失败并列出精确artifact
  - _Requirements: 6.4, 6.5, 10.3, 10.5, 11.2, 11.3, 11.4, 11.5_
  - _Boundary: Frozen Distribution Visibility_
  - _Depends: 7.3_

- [ ] 7.4a 在同一发行候选完成并合并owner frozen revalidation
  - 以7.4已验证的同一dist依次闭合WA-01 `5.12b`、WA-02 `1.4b/4.5b`、WA-04 `5.5a`、WA-05 `5.4a`、WA-06 `9.6b`及WA-07 `6.6b/7.4b/7.6b/9.2a`；每项都必须运行真实packaged consumer/API，不得由1.6最小spike、source证据或mock代答
  - 核对每个owning Spec的route approval、amendment commit、merge identity、同一bundle manifest/evidence SHA与失败安全语义；任一失败保持该row和下游WA-08 frozen journey为BLOCKED
  - 完成时各owner row推进为`FROZEN_REVALIDATED_PASS`，但只有WA-08 packaged journey闭合后才可进入terminal `MERGED_PASS`
  - _Requirements: 5.1, 5.3, 10.3, 10.5, 11.2, 12.3, 12.5_
  - _Boundary: Post-build Frozen Owner Revalidation Merge_
  - _Depends: 7.4, WA-01 5.12b, WA-02 1.4b, WA-02 4.5b, WA-04 5.5a, WA-05 5.4a, WA-06 9.6b, WA-07 6.6b, WA-07 7.4b, WA-07 7.6b, WA-07 9.2a_

- [ ] 7.5 验证并合并 WA-08 Qt increment amendment
  - 核对Qt owner approvals/task suffix/commit/merge evidence，运行单JSON editor、bundle resources、qwindows/visible window、avatar功能回归和clean-user journeys
  - 保持Layer 4/Controller boundary、keyboard/accessibility与旧Qt baseline；不得把Windows平台逻辑散落进widgets
  - 完成时，WA-08与WR-03 evidence闭合，所有required amendment rows均`MERGED_PASS`
  - _Requirements: 5.1, 6.2, 6.4, 6.5, 7.1, 11.2, 11.3, 12.5_
  - _Boundary: Qt Windows Journey Amendment Merge_
  - _Depends: 0.3c, 7.4a, qt-editor-json-mvp-increment 5.2b, qt-editor-json-mvp-increment 5.3b, qt-editor-json-mvp-increment 5.4b_

- [ ] 8. 在clean Windows发行环境运行分能力packaged E2E

- [ ] 8.1 验证真实windowed EXE、qwindows与诊断路径
  - 从clean user profile、清除PYTHONPATH/Qt developer paths、非仓库CWD启动真实EXE，创建可见main window并进入event loop
  - 分别移除插件/asset/source proof副本验证稳定诊断；windowed模式用受控marker/log/exit helper取得结果
  - 完成时，正常启动PASS，任何mandatory缺失均明确FAIL而非静默退出
  - _Requirements: 6.2, 6.3, 6.4, 11.3, 11.5, 12.1_
  - _Boundary: Packaged Qt Startup_
  - _Depends: 7.5_

- [ ] 8.2 验证packaged项目保存/退出/重开
  - EXE创建/编辑/保存项目，完全退出后重新启动并重开，比较受合同保护content/metadata/authority
  - 运行target-open、双实例、junction/ancestor swap、save fault与recovery；不得读取checkout或调用venv Python业务模块
  - 完成时，正常journey与old/new/recovery-only矩阵在真实dist上通过
  - _Requirements: 7.1, 7.2, 7.3, 7.4, 12.1, 12.2_
  - _Boundary: Packaged Project Lifecycle_
  - _Depends: 8.1_

- [ ] 8.3 (P) 验证packaged TM激活/退出/重启恢复与并发
  - EXE首次激活合法TM，验证唯一canonical generation；完全退出/重启后恢复相同authority并查询
  - 运行双EXE竞争、kill、activation fault、attestation tamper/FileId reuse与power-cut recovery；未知事实不回落legacy
  - 完成时，single authority/restart/LKG/recovery与safe UI state全部通过
  - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 12.1, 12.2_
  - _Boundary: Packaged TM Lifecycle_
  - _Depends: 8.1_

- [ ] 8.4 (P) 验证packaged TMX导入与FTS5持久检索
  - 从rooted source导入有效TMX并核对count/locale/conflict/canonical bytes；escaped/reparse/swap source保持target零变化
  - 在published SQLite创建/查询FTS5 trigram，退出EXE后重启并查询相同结果；不以compile-option字符串代替行为
  - 完成时，TMX与FTS5正常/失败矩阵在真实dist全绿
  - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 12.1, 12.2_
  - _Boundary: Packaged TMX and FTS5_
  - _Depends: 8.1_

- [ ] 8.5 重算frozen Gate并验证完整source/resource visibility
  - 在dist内重算Gate A/C/D与benchmark contract，逐项验证raw `.py` retained-handle/executed-byte binding、fixture handle identity/digest、Boot TCB、data/assets和bundle root
  - 修改manifest/source/fixture、插入bundle junction/reparse、制造PYZ duplicate或从checkout提供缺失文件都必须fail closed
  - 完成时，source runtime与frozen Gate结果符合approved contract且zero mandatory skip
  - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 11.5_
  - _Boundary: Packaged Capability Gates_
  - _Depends: 8.1_

- [ ] 9. 闭合跨平台回归、清单、日志与独立Feature GO预审

- [ ] 9.1 运行direct-platform-primitive与frozen-closure静态门
  - production consumers不得直接import/use `fcntl/flock/dir_fd/O_DIRECTORY/O_NOFOLLOW/directory fsync`或Win32 wrapper；仅platform adapters/approved bootstrap可出现
  - frozen roots与代码dependency graph双向核对，任何新增critical module/fixture未入manifest即失败
  - 完成时，命中均有owner-approved exception或为零，不能以平台条件分支隐藏
  - _Requirements: 1.1, 5.2, 5.4, 10.2, 12.3_
  - _Boundary: Platform Boundary Static Gate_
  - _Depends: 8.2, 8.3, 8.4, 8.5_

- [ ] 9.2 在同一commit运行macOS/Linux全量回归与POSIX parity
  - 使用任务2.2 characterization和现有CI矩阵验证Parser/Chunk/Project/Resource/TMX/TM/Qt public codes、bytes、recovery和Gate不回退
  - 平台专属差异只能是approved backend facts；不得把Windows pass换成Mac/Linux skip或改变authority
  - 完成时，同一full commit获得fresh artifacts，mandatory失败/skip阻塞Feature GO
  - _Requirements: 1.4, 5.3, 5.4, 12.3, 12.5_
  - _Boundary: macOS Linux Regression and Parity_
  - _Depends: 9.1_

- [ ] 9.3 生成完整通过/失败矩阵、Windows适配与frozen packaging清单
  - 汇总baseline→candidate、环境/versions、exact commands、exit/log/checksum、filesystem/lock profiles、power-cut、consumer journeys、source/fixture/resource/Qt inventory
  - 每项标PASS/FAIL/NOT_RUN与阻塞reason；required项不得以source-only pass、mock、skip、手工patch或推断转绿
  - 完成时，portable evidence manifest可复核所有日志且没有本机绝对路径作为authority
  - _Requirements: 11.2, 11.4, 11.5, 12.1, 12.2, 12.3, 12.4_
  - _Boundary: Final Reproducible Evidence Package_
  - _Depends: 9.2_

- [ ] 9.4 完成累计对抗性实现评审与最终治理差异核对
  - Cumulative reviewer从Requirements/Design/ADRs/ledger反向检查实际tree/runtime，重放Boot TCB/executed-byte binding、share/CAS、ACL/FileId、durability profile、Windows source/EXE与Mac/Linux红线
  - 检查所有WA merge identity、WR evidence、Steering sync与实际delta；实施期新增跨门槛事实必须回到ADR/Spec审批，不能用Implementation Notes补授权
  - 完成时，无unresolved blocker/major、无未批准delta、diff/checksum/branch tree一致；否则保持NO-GO
  - _Requirements: 1.2, 5.1, 10.3, 12.2, 12.3, 12.5_
  - _Boundary: Independent Implementation and Governance Review_
  - _Depends: 9.3_

- [ ] 10. 完成Windows onedir/windowed最终发布验收

- [ ] 10.1 在同一clean发行候选上完成LocalCAT启动、项目保存/重开、TM激活/重启恢复、TMX导入与FTS5最终闭环
  - 从全新Windows user profile、非仓库CWD、无Python/Excel依赖启动真实`LocalCAT.exe`并确认可见Qt窗口与`qwindows.dll`
  - 依次保存/退出/重开项目，首次激活TM/退出/重启恢复，导入rooted TMX并验证count，创建/查询/关闭/重开FTS5 trigram；随后执行双进程锁、target-open、kill/recovery和approved power-cut lane
  - 对同一dist重算frozen source/Gates、校验fixtures/data/resources/ico/version与avatar功能回归；归档exact commands、完整日志、PASS/FAIL矩阵、Windows FS/lock清单、frozen-source/packaging清单与SHA-256 inventory
  - 完成时，所有mandatory项在同一commit/dist上PASS、zero skip/patch/mock，WA/WR ledger与Mac/Linux回归闭合，Governance owner才可将Windows状态从NOT_VERIFIED改为VERIFIED；否则保持NO-GO
  - _Requirements: 1.1, 1.2, 2.1, 3.1, 4.1, 4.2, 5.1, 6.2, 6.5, 7.1, 7.2, 8.1, 8.2, 9.1, 9.3, 9.4, 10.1, 10.2, 11.1, 11.2, 11.3, 12.1, 12.2, 12.3, 12.4, 12.5_
  - _Boundary: Final Windows Packaged Feature GO_
  - _Depends: 9.4_
