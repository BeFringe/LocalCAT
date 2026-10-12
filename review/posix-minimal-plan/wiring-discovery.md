# POSIX TM 最小修复接线发现

日期：2026-10-12。静态源码基线：开发 HEAD `2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5`；外援输入：`b3d5e26:REVIEW-POSIX-SQLITE-BINDING.md`。本文件是供主 Agent 收束设计的发现记录，不是正式设计、审批或实现。本次只读源码；未运行产品实验、未恢复撤回候选、未改变生产文件。下述“建议”均待 owning R/D/T 审批后实施。

## 1. 首选路线的具体规模

四个接缝必须一起闭合：**平台分派、同资源家族生命周期、原 root 的配置位置关联、应用自己的 rooted 发布**。保留 stdlib SQLite、POSIX v2/Windows v3 codec、既有 coordinator 与 activation/recovery phase。它不是只改 `tm_migration` 中一个 `if`，但也不需要另建 VFS 或平台层。

三个正常场景中的设计风险需要显式处理：

1. 通用 backend 被当作 Windows v3 的开关不只发生在 initial recovery，还发生在 replacement、seal 和 publication。
2. 现有 W1 只覆盖若干激活/导出操作；普通 SQLite query/append 的 coordinator lease 是进程内计数。若仅补目录复证，两个遵守旧入口的 LocalCAT 使用者仍可能一边查询/写回一边换 canonical inode。
3. POSIX v2 要求发布的是原 sealed stage 对象；现有平台 publisher 只接受 `CandidateFile`，通用 mutable reservation 不能直接交给它。现有 portable seal 又产生另一种 attestation，因此不能仅切换 journal 版本。

本路线只能拒绝检查点已存在的漂移。真实 SQLite 连接仍按 pathname 工作，前后复证不覆盖内部所有 syscall、ABA 或活事务外部替换。失败状态不得因此假定 `unchanged`。

## 2. 分派清单与选择边界（静态确认）

| 入口/当前位置 | 当前行为与缺口 | 最小接线建议 |
| --- | --- | --- |
| `tm_resource_port.py:307/382` `replace_snapshot/create_snapshot` | 每次新建 `TMEngine`/coordinator，将通用 backend 传给 Core | ResourcePort 仍只调用 Core 完整事务；借同一已打开资源 owner，或由应用组合维持同一资源会话。不能在检测失效后新建 engine 清除锁存 |
| `tm_migration.py:2756` `_acquire_initial_reservation` | backend 缺省 POSIX 走旧 descriptor reservation；有注入则保留 generic root/W1 | backend 表示 I/O 能力，不表示恢复格式；POSIX 也保留 generic reservation，不能设为 `None` |
| `tm_migration.py:3391` `_recover_existing_initial_activation` | `_backend is not None` 即 portable initial recovery | composition 明确 POSIX v2 / Windows v3 recovery 路线；POSIX 的旧 v2 owner 接收原 family borrow，不重新取得路径授权 |
| `tm_migration.py:3240/3251/3282/3296` initial transaction | 用 `attempt.platform_backend is None` 选 journal/publish/终态和失败清理 | 将“有 rooted authority”与“采用 v3 codec”分开；同一 attempt 全程使用一次已选路线 |
| `tm_migration.py:7086` `apply_snapshot_payload` replacement 分支 | 无条件调用 `_portable_explicit_disambiguation_reserved` | 增加真正的 POSIX reserved replacement 接线，内部复用 `activate_replacement`、v2 journal/recovery；不得回到释放 W1 后的 public legacy 重入 |
| `tm_migration.py:8282` replacement reserved | `portable_replacement_recovery_inputs`、`portable_journal_inputs`、`activate/publish_portable_replacement` 全为 v3 | 共用 source/build/reservation 阶段，codec-specific activation 仍由原 owner 选择；不抹掉 Windows proof 参数 |
| `tm_migration.py:10733/10873/10930` journal/recovery/runtime inputs | 明确 `narrow_windows_*`，真实 capability 缺失会拒绝 | 这些保留为 Windows 专用；不要改名为通用并静默绕过证明 |
| `tm_stage_sealer.py:4401/4441` `seal` | `platform is not None` 就 `_seal_portable`，其产物为 portable attestation | 让 rooted I/O 与 attestation codec 正交；POSIX 使用既有 v2 attestation，读取/同步借原 family authority；Windows 保持原 portable closure |
| `tm_migration.py:7558/7597` schema upgrade | reserved portable upgrade 同样借 v3 journal/runtime | 受本次资源族 lifecycle 改动影响的 reopen/backup 接上同一 borrow，保留 codec 分支，不顺带改 schema |

选择点宜在现有平台 composition/owner construction 一次确定，并将明确值传进 migration/coordinator；不能在任意异常后“试 Windows 失败再 fallback POSIX”。`FileObjectIdentity.platform` 可描述对象，不能成为 caller 随意选择恢复合同的输入。

## 3. 一次 import 的原 authority 流转

现有可复用结构：`_InitialActivationResourceReservation`（`tm_migration.py:10121`）持有 root、W1 lease、固定 lock 名/payload；`bound_family_inputs`（10540）借用这组对象；`_freeze_initial_stage_attempt`（11530）已在同一 root 下排他创建 DB/manifest reservations；`_CallerHeldSealBorrow`（`tm_stage_sealer.py:134`）是不可序列化、不负责关闭外层 root/W1 的借用。

建议贯通顺序：

1. 由现有应用资源会话识别 resource/target，进入该资源 admission；replacement 在发布配置 source **之前**阻止同家族的新操作并 drain，而不是先改 JSONL、到 `activate` 才 drain。当前 `apply_snapshot_payload`（6942）会先发布 source，后进入 activation，这是必须前移的生命周期边界。
2. 取得该资源既有 W1；在原 root 上复证配置位置、lock binding、canonical/source baseline。payload 是独立私有输入 root，不可成为目的地 authority。
3. 在原 source root 创建恢复副本和输入 candidate，保持 `PendingPublication` 到 Core/registry terminal。create/replace 准入、摘要/计数与包输入校验不变。
4. `_freeze_initial_stage_attempt` 在原 root 排他创建 stage/manifest；`_build_stage`（9569）把原 family borrow 继续传给 `initialize_stage_schema`、stage `SQLiteTMStore` 与其所有短连接。不能只传 `initialize_stage_schema`，随后构造一个无 borrow 的 store。
5. 每次真实 connection 开始前、事务入口和 SQL 阶段后复证；所有相关 SQL/cursor/backup 完成并关闭后，才做原始 DB FD 的 hash/copy/sync/关闭。stage reservation 在此期间可保持已打开但不能提前 close；仅 `commit` 不等于连接关闭。
6. Sealer 使用原 root 打开 source/stage/manifest；当前 `_open_portable_stage_live_authority`（694）会重新 `bind_root(source_path.parent)`，应改成 caller borrow 开原 source，不能接受同路径新 root。v2 的旧 proof 形状保持不变，但 proof 必须由原 authority 对应的对象采集。
7. 现有 coordinator 激活、v2 phase owner 仍负责状态机，应用自己涉及 journal/lineage/backup/final/cleanup 的命名操作消费同一 root/W1。stage 同对象发布见第 5 节。
8. receipt/active metadata SQL 更新、final 语义重开、source publication terminal、registry/owner commit 均在外层 borrow 未释放前执行。`project_applied_owner`（3914）已有避免另开 `TMEngine` 的借用投影，继续复用。
9. 先关闭内层 SQL/cursor/backup；再关闭本次 pending/file/seal/retirement authority；终态复证成功后释放 W1，最后释放本操作 borrow/admission。原 resource session 的 root 继续作为下一次操作的 identity anchor，直到明确关闭会话；不能下一次直接重绑同 pathname 刷新旧授权。

异常时：漂移锁存优先于 cleanup/重试；不再执行 pathname restore、递归清理、删除 journal 或自动接管。可以按原 authority 且已获证的范围结束/保留自有候选，但 root 位置复证失败时默认保留恢复物。关闭 SQLite 仍由其 owner 完成，不能宣称 rollback/close 无内部 I/O。最终结果必须区分已提交、已证明 prior、或 unknown/recovery-required。

## 4. 协作并发与锁顺序：必须验证的最小机制

### 现状

- `ResourceStoreCoordinator._operation_lease`（`tm_sqlite_store.py:4030`）只是当前 coordinator 的 active count。
- drain（`_CoordinatorStorePort.drain_for_transition:2078`，`_activate:5274`）只等待本实例 count；没有跨进程参与者。
- `_open_leased_connection`（18197）直接按 path 开 SQLite；不取得 W1。
- POSIX `_acquire`（`platform_fs_posix.py:2787`）每次独立 `os.open`；`_flock`（2927）全部 `LOCK_EX`；`LockPolicy`（`platform_fs_contracts.py:295/311`）只有等待方式，没有 shared 或递归模式。因此不能把第二次 acquire 当作“同线程可重入”。
- ResourcePort 与 `_open_runtime_binding`（`tm_application_composition.py:1043`）会分别创建 `TMEngine`；它们不是天然共享同一 coordinator。

### 首选有界接线（建议，不是已实测）

复用现有 W1，为每个外层资源操作持有排他 lease；同一次操作内部 stage/health/receipt/recovery 显式借用它。采用既有有界等待/忙错误，不新加共享锁平台。此方案是短时序列化同资源操作，**不是资源永久单 owner**。它可能影响同资源重叠查询时的等待，需与现有并发合同和取消预算对齐后批准。

取得顺序应统一：

- 普通 query/append/export：短持 coordinator condition 取得 admission/active count → 释放 condition → 获取外层 W1 → 原 root 位置与代次复证 → SQLite/应用操作 → 所有连接及原始句柄收尾 → terminal reproof → release W1 → active count--。
- replacement/recovery：在 condition 下阻止新本地操作、设 DRAINING → 释放 condition 并等待现有 active count 归零 → 获取 W1 → 再复证 generation/原 root → source/stage/publish/recovery → terminal → release W1 → READY 或 fail-stop。
- **禁止持有 W1 等待本地 drain**：已有 query 可能计入 active count 并正在等 W1，反向顺序会死锁。
- **禁止持 condition 阻塞等待 OS lock**：在途操作退出需要该 condition。
- 内部嵌套通过 resource/operation/thread 绑定的不可独立授权 borrow 复用外层 lease；不同线程不因路径相同而复用，同线程也不能只凭一个 thread-local pathname 静默取权限。
- 根位置 anchor 与失效锁存必须由同一进程资源会话持有，应用组合向 ResourcePort 和 runtime 传同一 owner；若存在多 coordinator，需窄的同家族会话/入口协调而非各自新建根。不要把全进程全局锁加到其他资源。

同进程原始 FD 与 SQLite 的互斥是上述 outer operation 的另一用途：有 SQLite 连接时不得让另一操作开/关同 inode 的 raw DB handle。一个 operation 内也要严格划分 SQL 阶段与 raw-file 阶段。`_store_health_snapshot`（17447）当前在 SQLite BEGIN 内进入 `_portable_health_database_window`，其 ExitStack 在 connection.close 前退出；这套 Windows shared-read 行为不能原样移植给 POSIX。POSIX 要在外层 W1 内完成 SQL 并关闭，然后 rooted hash；仍需通过语义事实、generation 和内容核对关联同一个对象。

取消/失败：有界等待到期或尚未获得 W1 时不能留下 active count；已取得 lease 的取消必须先让本 operation 的 SQL/cursor/backup 正常退出再 release。drain 期间取消可恢复 READY 的前提是未 mutation 且未发现漂移；漂移锁存不能由 finally 无条件改回 READY。真实 query view 会懒持 connection（`SQLiteTMQueryView._candidate_connection:17616`），其 `_invalidate` 必须在 outer borrow/lease 释放前执行。

静态尚不能证明这个接线满足所有重叠调用与取消；优先做第 8 节实验 B，不先宣布并发已解决。

## 5. stage 同对象交接给现 publisher

事实：`MutableFileReservation`（`platform_fs_contracts.py:1143`）仅有 identity/reprove；`CandidateFile`（1598）记录已写入/已 flush 的事实；`BoundDirectoryAuthority.begin_publish`（1937）只接 Candidate，命名后关闭 candidate 再返回 retained destination。POSIX `_rename_candidate`（`platform_fs_posix.py:704`）已经用 dirfd 做 link/unlink 或 replace，并验证 lease/parent。它可作为现有最终 publisher。

建议增加**从本平台创建并持有的 mutable reservation 单次转交为 publishable candidate**的窄接缝，放在既有 platform contracts/adapter 内：输入必须是原 root 下的 reservation 与已核验内容事实，由实现重新核对原对象、内容及同步；不接受任意 Path、裸 identity 或 caller 自报 flush。成功后旧 reservation 被消费，返回同一个 inode 的 CandidateFile；失败按“尚未转交/已转交但 publication 未闭合”明确所有权。对外 writer 的 SQL 结束由 Core family lifecycle 保证，平台并不声称能观测任意外部进程停止写入。

这样 v2 `_replace_activation_database`（`tm_activation_recovery.py:3821`）与 `_publish_activation_manifest`（4099）可继续要求发布前后身份相等，核心变为借原 root 调用现 publisher；不使用 `_PortableStageLiveAuthority.copy_asset_to_new_candidate`（`tm_stage_sealer.py:458`），后者复制出新 inode，不能满足 v2合同。

publication 本身消费/关闭 candidate，所以不应让旧 attempt finally 再 retire/close 已转交 reservation；必须在单次移交点把 attempt 的所有权字段转移。DB 发布后 receipt SQL 会改写 canonical，不能跨原 PendingPublication 的内容不变式。最终候选接线见 [PLAN §3.6](PLAN.md#36-db-命名子阶段与-receipt-sql-的交接)：先以原root完成DB_REPLACED durable journal，在pending仍live时取得并比较同对象mutation guard，pending terminal/close完成后才SQL。guard覆盖SQL及收尾，POSIX复证不另开关同inode句柄，SQL关闭后才采集final内容。原root/W1持续；冷恢复由原v2 owner核验完整closure后取得等价guard，不能凭Path收养。该接线需ADR-020 §9的明确POSIX子阶段限定，不能作为未修改合同已支持的事实。

### v2 proof 接缝仍待精化

`ContentFileProof`/`LegacyPosixContentFileProof`（`tm_content_attestation.py:104/121`）持久保存 v2 的 device/inode；通用 `FileObjectIdentity`（`platform_fs_contracts.py:114`）则明确 opaque/live-only。不得让 Core 解码 `volume_id/file_id` 字节或取平台私有 FD 拼出 v2 proof。

应在现有 POSIX 兼容 proof owner 与 rooted content capture 之间定义内部适配：旧 codec 不变，原 authority 做实际采集/比较，Core 消费已有 proof 类型。这个适配涉及 owner 边界，必须在正式设计明确归属和 factory，而不是给所有平台暴露数字 inode API。实验 A 必须连过 seal→v2 journal→同对象发布→冷重开才能证明可行；若无法不扩张接口地贯通，报告此正常 create 场景的具体缺口，不直接转向 VFS。

对比现有接口后的具体候选：

- `_capture_content_file`（`tm_content_attestation.py:1350`）确实能生成旧 v2 proof，内部从 `/` 到文件逐层 no-follow、哈希和前后 stat。但它每次根据 Path 新建整个链，不能借用外层原 root，因此不能直接调用它并假定已保留原授权。
- `_capture_platform_content_file`（1611）和 `_LivePortableContentCapture`（1542）使用 `BoundRegularFile.content_facts()`，拿到同一个 retained file 的完整摘要/大小/opaque identity；但 helper 会新 `bind_root` 且只返回 portable proof。首先应给现有 content owner 加“借已有 root/已打开 file”的内部 capture 入口，明确不关闭外层 root。这解决原 authority 传递，**仅靠当前返回值仍生成不了 v2 的 device/inode**。
- `BoundContentFacts/EntrySnapshot/PublishFacts` 以及目录 observation 现有公开值都只有 opaque identity；没有可以直接复用的 numeric POSIX stat 观察接口。`platform_fs_posix._identity_from_stat:132` 知道数值如何编码，也不能据此让 Core 反向解析。
- 因而保留 v2 时，最窄候选是在现有 POSIX adapter 增加**受已持有 BoundRegularFile 限定的 legacy observation**。它只接受由本 adapter 在原 root 签发的 live file authority；内部通过同一个 file descriptor 的 fstat、已验证内容读取和父链复证，输出 `(device, inode, size, digest)` 的只读观察值，并同时保持原 opaque identity 比较。不返回 FD、不接受任意 Path/裸 identity、不形成单独写权限。
- `tm_content_attestation` 继续作为 v2 factory owner，使用该 observation 构造现有 `LegacyPosixContentFileProof`；platform 不导入 TM schema。这个能力是 POSIX 兼容 codec 的可选内部接缝，不加入所有 backend 的通用必需能力，不另建持久 schema 或平台层。Windows 继续使用 portable content facts。
- 旧数字只是为既有 v2 codec 保存的观察数据，不能独立赋予跨重启授权。冷恢复仍须本次 root/W1、内容、lineage/receipt 与 codec 校验联合复证；数字相等、同字节新 inode或数字迁移均不自动接管。此候选必须作为 owning platform/Core 设计差异接受审阅，不能以“仅传几个数值”跳过边界裁决。

这比让 content owner 自己重走新 Path 链更能保持原 root，也比更换 portable proof 或复制 stage 更小。它是否足够覆盖所有 v2 journal/attestation 消费者，需实验 A 而非静态宣称。

## 6. 真实连接与应用 I/O 覆盖表

| 链路 | 必须接线/验证的具体入口 |
| --- | --- |
| builder/schema | `_build_stage:9569`、`initialize_stage_schema:18315`、随后 `inspect_stage_schema`；builder 新建 store 的所有 append、revision、issued receipt 连接都继承同一 borrow |
| 普通 SQL | `_open_configured_connection:18045`、`_open_leased_connection:18197`、query view lazy connection `17616`；canonical 已存在时固定 `mode=rw`，不能遗失文件后自动造空 DB |
| Seal | `tm_stage_sealer._open_stage_read_connection:1482`、write:1501、`_restore_stage_unpublished:2816`，以及验证过程内部 reopen；读可能触发 SQLite hot recovery，不能按 SELECT 认定无写入 |
| Receipt/active validation | `tm_activation_recovery._publish_activation_receipt:3955`、`_complete_recovered_receipt:5888`；`_CoordinatorStorePort.open_configured_connection:2142` 必须传家族上下文，不只传 path |
| Completed authority/health | `tm_sqlite_store._open_completed_authority_read_connection:3086`、`_open_health_connection:18230`、`_store_health_snapshot:17447`；immutable completion 仅保留既有无辅助文件前提，不推广到一般 runtime |
| Schema backup/upgrade | `tm_schema_upgrade._create_schema_upgrade_backup:178`（source/destination backup 双连接）、`_schema_version_of_store:807`、`_open_legacy_read_connection:861`、`_copy_store_into_stage:1007`；`tm_sqlite_store:19375/19418/19451` 的额外只读入口；backup 源/目标连接都结束后才 hash/close raw FD |
| Migration旁路直连 | `_reject_sidecar_reuse:9783`、`_validate_reusable_stage:11078`，不能漏掉 schema/version/preflight |
| Cold runtime/recovery | `tm_engine.open_canonical_tm_store:264`、`_rehydrate_canonical_runtime_authority:240`；`ResourceStoreCoordinator.recover_durable_activation:9779`、reattest/rehydrate 系列；第一次 pathname DB 读取前先取得本次 cold family/W1，不能先读出 store id 再当成已授权对象 |
| 应用 publish/recovery 文件 | `tm_activation_journal` 的 capture/read/sync/replace/write journal、terminal、lineage、quarantine（例如2260/2731/3360/3507）；`tm_activation_recovery._restore_activation_file:4664`；`tm_schema_upgrade` pending backup 与 cleanup；不只改 DB/manifest 两个 replace |
| Snapshot export/refresh | `tm_migration._publish_bound_export_snapshot:5496`、receipt completion、`tm_snapshot_artifacts`/`tm_snapshot_recovery` 原恢复 owner；来源 canonical 与 destination 可为两个目录，目的地独立 root 不可替换来源 family |

`tm_activation_journal.py:2731` 当前 `_replace_activation_file` 仅 `os.replace(source,destination)`；journal/terminal write 也仍 pathname。这些是已有 owner 的 I/O 接缝，不应复制整套 phase machine。复用内部 port/可选原 authority 参数，将临时创建、精确读、publish、sync、cleanup 转给现平台 primitive；任何生产修改都需 owner 测试明确实际选择了此路径。

## 7. root 配置位置与失效隔离

平台事实：`PosixPlatformAdapter._bind_root`（2236）只打开配置目录本身，初始 chain 为 `(fd,), (None,)`；`_reprove_directory_chain`（283）第一项只 fstat。因此目前证明 retained root 活着，不能证明配置位置仍指向它。

最小增强在既有 POSIX adapter：保留原配置位置关联，从稳定祖先的 retained 链比较名字→对象，再比较原 root；所有从 root 派生的目录/file/lease 继承这个关联。不要每阶段重做 `bind_root(config_path)` 然后取新对象替换旧 anchor。必须先明确已有路径 normalization/允许系统别名的边界，避免把 Mac `/var`→`/private/var` 这类既有合法入口因遍历方式变化静默禁用；也不通过新的 `resolve()` 追随已换目录。此细节属于原 platform binding owner，Core 不读取 FD。

检查点：W1 到手后、首次 mutation 前、真实 SQL connection/transaction 入口、SQL完成后、应用 publication 前后、owner/registry success 前。检查结果是当前关联，不是名字冻结。

已有可复用机制：

- coordinator state `READY/DRAINING/ACTIVATING/FAILED` 与 `_operation_lease` 拒绝新工作。
- initial activation 的 `_initial_activation_authority_unavailable` 锁存（3890以后）与正式 recovery 才清除的规则。
- `_current_index_identity`（3838、17399）健康失败保留旧对象 anchor，避免第二次验证把替换对象洗白。
- `_refresh_observation_gate`（4068）按资源协调 source/manifest 两步窗口；不是全局锁。
- Resource status/health、`MigrationFailure.canonical_authority_published/ambiguous`、ResourcePort 的 `RESOURCE.IMPORT.RECOVERY_REQUIRED` 可承载失败，不新增验证按钮。

需要补的只是资源会话级“配置位置失效”锁存与旧 family borrow 的撤销。它不是 `SOURCE_DIVERGED`：后者是源 JSONL/manifest 内容关系，不能把路径失效写进可疑 SQLite 后再说成功锁存。新 query/write/import、fresh engine callback、receipt success 都应检查同一会话状态；其他资源及项目保存不受该锁存全局阻断。冷重开由原 recovery owner 创建新的会话并核验真实家族，不靠持久 inode 赋予跨重启授权，也不承诺自动修复外部替换。

## 8. 优先的有限实验与退出条件（尚未执行）

| 实验 | 具体正常场景/不确定性 | 通过与停止条件 |
| --- | --- | --- |
| A：原 owner 全链路 | 合成少量数据，经实际 ResourcePort create/replace→stage→POSIX v2 seal/publish→owner callback→cold reopen | 原 root/W1 被一路借用，stage 同 inode 发布、codec不变、receipt/registry/source count一致；若v2 proof无法与rooted capture闭合，报告接口缺口，不声称分派修复完成 |
| B：生命周期 | 一个进程两个 coordinator/两个线程及两个进程，同资源 query/append 与 replace；含懒 query view、嵌套 health、timeout/cancel、关闭后重开 | 无递归 W1、自等 drain、漏 active count、额外 raw close 导致竞争连接提前写入；旧 generation 不能写回新库；第二参与者有界等待/忙且可重试。若短排他影响已承诺的正常并发，先报告该影响再另裁决，不永久单owner绕过 |
| C：同对象交接 | reservation→Candidate→publisher；DB receipt 更新；发布前/命名后/terminal失败 | 单次消费；失败所有权明确；原 sealed identity 与 final一致；无 copy替身、重复 cleanup；每个 raw DB close 发生在相关SQL结束后 |
| D：既有崩溃矩阵 | PREPARED、DB_REPLACED、MANIFEST_PUBLISHED、GENERATION_PUBLISHED 必要切点；正常热日志、schema backup/cold recovery | 原 phase/lineage/receipt恢复规则继续可用，旧/新完整状态可判；不删除不明日志，不改旧失败证据为PASS |
| E：已观察替换 | 在真实 `_build_stage` 入口前完成目录rename＋同字节clone，再继续真实builder；另测首次mutation前和publish前 | 在旧authority失效处拒绝，clone无应用发布/receipt/registry成功；原恢复物保留。不用mock直接抛异常替代实际代码 |
| F：边界观察 | SQLite INSERT后外部进程替换目录，记录两家族与close效果 | 明确为不支持时序/残余风险；检测后无新操作或虚假成功，不要求全字节无副作用，也不自动升级首发硬门 |

Windows只对实际共享接口、reserved handoff和lifecycle变化做普通frozen回归；Mac/Linux保持source。资源同步未来只复用同一Core资源事务入口，输入是一致包/批准快照；不复制活动SQLite/journal，不在此建立同步平台。

## 9. 建议给主设计的最小结论

可以继续这条较小路线，但当前已有 primitives 不能证明所有接缝天然闭合。正式设计至少应明确：v2/v3选择值、资源会话/borrow归属、admission/drain/W1顺序、SQL与raw FD阶段、同对象reservation交接、v2 proof的rooted采集接缝、配置位置失效锁存与恢复出口。其余复用现有owner。先做有限全链路与并发实验，不能只改 helper 或逐个加 stat 后报告完成。

## 10. 供设计评审选定的窄签名候选

以下是可执行的接线形状，不代表已批准名字或新框架；可以在现有文件中私有实现。首轮实验只需选定两个技术门：**W1/双 coordinator 顺序**、**POSIX v2 observation＋同对象交接**。失败先报告正常场景具体障碍，不扩成完整 VFS 研究。

```python
# tm_sqlite_store.py：现 coordinator 的内部生命周期接缝。
# _ResourceFamilyBorrow 是一次外层操作的不可序列化借用，不是额外服务。
ResourceStoreCoordinator._family_operation(
    *,
    caller_borrow: _ResourceFamilyBorrow | None = None,
    transition: bool = False,
) -> AbstractContextManager[_ResourceFamilyBorrow]

# 仍返回原 sqlite3.Connection；不把它改成驱动代理。
_open_configured_connection(
    database_path: Path,
    *,
    expected_file: _ReservedStageFile | None = None,
    require_existing: bool = False,
    family_borrow: _ResourceFamilyBorrow | None = None,
) -> Iterator[sqlite3.Connection]

# tm_migration.py：原 reservation 已取得时，不再重新取得 family/W1。
_InitialActivationResourceReservation.family_borrow(
    coordinator: ResourceStoreCoordinator,
) -> _ResourceFamilyBorrow

# platform_fs_contracts.py / platform_fs_posix.py：可选 POSIX 内部能力。
# 观察数据不是 mutation authority，也不返回 os.stat_result/raw FD。
observe_legacy_posix_content(
    root: RootedDirectoryAuthority,
    file: BoundRegularFile,
) -> _LegacyPosixBoundContentObservation

# observation：device:int, inode:int, content:BoundContentFacts。
# content仍含opaque live identity；device/inode仅供既有v2codec字段。

# 现有 MutableFileReservationService 的窄 companion 接缝。
# 原 reservation 由 parent 创建，SQL及其他外部writer已由Core结束。
finish_mutable_candidate(
    parent: BoundDirectoryAuthority,
    reservation: MutableFileReservation,
    expected_content: CandidateContentFacts,
) -> CandidateFile

# tm_content_attestation.py：既有 content owner，不取得新root。
_capture_legacy_posix_bound_content(
    backend: _LegacyPosixContentObservationPort,
    root: RootedDirectoryAuthority,
    file: BoundRegularFile,
) -> LegacyPosixContentFileProof
```

`caller_borrow=None` 不应成为生产路径忽略家族协议的 fallback：非资源内存探针可以无需 family；TM stage/canonical 实例创建时必须指定原 owner，connection helper从它获得borrow。实现时可以不把额外参数暴露给全部外部调用，而在原 `_SQLiteGenerationView` 绑定同一 owner，语义要求相同。Codec选择单独使用内部枚举 `POSIX_V2/WINDOWS_V3`，不复用 nullable backend。

必要状态限定如下：

- coordinator/应用资源会话：原 backend/root、不能自动清除的 `binding_invalidated`、现有 READY/DRAINING 等状态与 active lease count。runtime 与 ResourcePort 必须复用此会话；首次create由Core创建后登记，明确close才解除会话。没有理由默认引入全局进程路径缓存；应用composition可向ResourcePort注入既有owner resolver。
- 单次family borrow：resource identity、所借原root/W1、owner thread/operation token、嵌套借用深度、是否关闭、当前SQL活动计数。borrow只能由原owner签发；跨线程/不同资源借用拒绝；取消退出也要归零。计数用于禁止在活SQL期间进入raw DB文件阶段，不是新持久证明。
- reservation/candidate移交：已有 `OpaqueAuthority._mark_authority_transferred`（`platform_fs_contracts.py:603`）与 `RetainedRetirement._accept_reservation_transfer`（1260）可借鉴单次转移规则。成功移交后旧reservation公有入口失效；candidate最终负责释放原保留FD及父链，不能复制FD后让旧reservation被外层finally提前关闭。`begin_publish`仍消费candidate并返回原有PendingPublication。
- cold恢复：没有活reservation可移交的旧stage，不能调用上面函数接收任意Path。须由现有严格恢复owner的已验证closure加原root签发一个受限的已有候选接缝；这不是create路径的默认入口。实验若需要它，明确只接现有v2record和当前对象证明，单次消费，不允许用户构造record直接提升权限。

双coordinator门可先用注入同一owner的真实runtime/ResourcePort实验，再做独立进程；若确有消费者必须独立coordinator，才评估在现有composition加入最窄家族会话协调。不能先做全局注册表、永久单owner或新的锁模式，再倒推需求。

## 最终候选补充：命名子阶段交接

现有 `PendingPublication`（`platform_fs_contracts.py:1754`）必须保留原destination且terminal内容等于preliminary；`publish_activation`（`tm_activation_recovery.py:8807`）已有DB_REPLACED后receipt SQL顺序。既有 `ExistingFileMutationGuard` 端口（`platform_fs_contracts.py:2591`）可作为后续同对象借用，但POSIX实现（`platform_fs_posix.py:2370`附近）当前拒绝能力，需要正式补齐。它只fstat/live名称复证，不能复制Windows临时DB句柄策略。精确阶段、失败与冷恢复合同以PLAN §3.6为最终候选；本发现稿不证明实现已存在。
