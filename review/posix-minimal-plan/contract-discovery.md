# POSIX TM 最小修订：owning R/D/T 合同映射

本文是供主审稿整合的只读合同发现与候选文字，不是正式规格。源码基线为 `2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5`；外援输入为 `temp-posix-sqlite-binding-review@b3d5e26` 的 `REVIEW-POSIX-SQLITE-BINDING.md`。未改代码、正式 R/D/T、ADR、历史完成项或 `spec.json`；没有运行产品或重新签发验收。

## 1. 最小 owner 范围

| Owner | 本轮所需变化 | 不扩大部分 |
| --- | --- | --- |
| `tm-storage-retrieval-index` | R 限定原无条件保全/回退句并增加资源位置失效行为；D 补全 POSIX 分派、同家族生命周期、真实连接/reopen、stage 同对象交接、应用发布与失败收尾；T 新增有界实施与验证项 | 不改驱动、journal mode、SQL/排序、schema、POSIX v2/Windows v3 持久格式、资格或性能门 |
| `windows-platform-enablement`（共享平台 owner） | R/D 区分 retained 对象有效与配置位置有效、通用 backend 与 Windows 特有恢复能力；D/T 增加 POSIX root 位置复证及已有 stage 窄发布接缝 | 不降低 Windows rooted/private-proof/durability；不新增平台层或 Mac/Linux frozen |
| `language-resource-portability` | R/D 写清 preview 首次 mutation stale 与在途漂移、成功与恢复事实；T 闭合实际 POSIX create/replace 调用链和安全输入回归 | package grammar/profile、artifact/receipt 格式、TM/Termbase owner 与 provider 边界不变 |
| `feature5-ui-integration` | 小幅 R 修订首次失败自动 legacy 回退前提；D/T 消费资源级停止及恢复要求、保留 peer 资源与项目编辑/保存 | 不新增目录验证按钮、资格、性能运行；ADR-016 device-only 维护与本轮位置漂移不能混用 |
| `cross-device-sync-plugin` | 只核对既有 R3、D“相邻合同与执行依赖”及 artifact adapter；当前已有只传完整包、回本机 validate/preview/apply、不传 live SQLite/journal 的合同，无新 R/D/T 必要 | 不在这次接线设计同步实现、网络 provider 或任意 raw-sidecar 迁移工具 |
| `rpy-project-codec` | 只沿既有 T6.2/T6.3 完成手工包和交付依赖；维持未完成状态直至实际贯通 | 不改 RPY 解析、源更新、格式导出或 Project 业务合同 |

当前 Core R2.4、R2.11–12、R7.5、R7.13–14 和 UI R5.6 有不带前提的“保留/继续提供”句。仅追加范围段而保留这些绝对句会产生合同冲突。下列替换保留正常失败承诺，只使已无法证明身份的资源进入明确的停止状态；不以这次修订重新解释历史 PASS。

## 2. Core Requirements 候选

定位：`.kiro/specs/tm-storage-retrieval-index/requirements.md`。

在“范围边界”增加以下轻量支持范围，Scope Lineage 指向既有 R2.4、2.9–2.12、R7.5、7.13–7.14，以及平台与资源包相邻合同；不在正文放审批沿革。

> **运行与迁移边界**：支持稳定本机资源目录中的正常使用、遵守 LocalCAT 资源协调流程的并发与替换、崩溃恢复、完整资源包导出导入，以及所有使用者退出后的正常迁移。运行期间由外部程序移动或替换活动TM目录、替换活动数据库或直接干预内部数据库／事务恢复文件不受支持；配置JSONL／manifest的外部内容变化仍按既有SOURCE_DIVERGED合同处理，canonical与其家族位置仍可证明时保留原Lookup/Update，逐文件异常仍按原严格规则拒绝。不承诺任意同用户竞争下全时序零副作用。该限制不豁免 LocalCAT 自身的错误访问、遗漏协调或正常恢复缺陷，也不放宽恶意输入、路径穿越、链接、摘要篡改及已观察身份失效的拒绝。离线迁移不把未经恢复的残留文件集合自动视为一致快照。

完整替换句：

- **R2.4**：If 迁移在解析、校验或写入期间失败且资源处于支持的运行环境, the LocalCAT TM 子系统 shall 保持原始 JSONL 字节和当前可用 TM 不变；If 已发现外部位置或身份失效, the LocalCAT TM 子系统 shall 按 Requirement 7 的资源级停止规则处理，不声明未经证明的原始资产仍未改变。
- **R2.11**：If 新版本激活或重新载入失败且存在仍可验证的先前活动版本, the LocalCAT TM 子系统 shall 报告失败阶段并继续提供该完整版本；If 先前版本的位置、身份或恢复状态无法证明, the LocalCAT TM 子系统 shall 停止使用该资源并报告恢复要求，不提供可疑版本。
- **R2.12**：If 首次迁移的新版本激活或重新载入失败、不存在先前 canonical 版本且原 JSONL 的身份与可用状态仍可证明, the LocalCAT TM 子系统 shall 报告失败阶段并继续提供原 JSONL 兼容能力；If 原 JSONL 或未完成激活状态无法证明, the LocalCAT TM 子系统 shall 停止使用该资源，不自动回落或接管同路径副本。
- **R7.5**：If 资源更新后无法重新载入且更新前最后一组查询结果的资源来源仍可验证, the LocalCAT TM 子系统 shall 保留该可用来源；If 该来源的位置或身份失效, the LocalCAT TM 子系统 shall 停止使用该来源并报告受影响资源。
- **R7.13**：If 显式 import 或 rebuild 未通过验证或激活且资源处于支持的运行环境, the LocalCAT TM 子系统 shall 保持 `SOURCE_DIVERGED`、last-known-good canonical TM 和发生分歧的原 JSONL 不变；If 已发现位置或身份失效, the LocalCAT TM 子系统 shall 保留可确认的恢复事实并停止该资源，不把可疑资产当作 last-known-good。
- **R7.14**：If 待激活 TM 的资源身份或来源绑定与配置资源不一致, the LocalCAT TM 子系统 shall 拒绝激活并标识受影响资源；先前版本仍可验证时保留其可用性，否则停止该资源且不得将同路径副本重新接纳为原活动资源。

新增 R7.15–7.18 候选（当前最后编号为 7.14）：

15. When 一次资源操作开始或进入改变资源的下一阶段, the LocalCAT TM 子系统 shall 检查配置位置仍对应本次已确认的资源，并拒绝已可观察的位置或身份变化。
16. If 操作中发现资源位置或身份失效, the LocalCAT TM 子系统 shall 停止该资源的新操作，保留可确认的恢复事实，不盲目覆盖、删除恢复文件或立即接管同路径副本，并且不把无法确认的提交结果报告为成功。
17. While 单个资源因位置或身份失效停止使用, the LocalCAT TM 子系统 shall 保持其他健康资源按各自状态可用，不把该失败扩展为全部资源失效。
18. When 用户需要迁移或恢复资源, the LocalCAT TM 子系统 shall 提供通过完整资源快照导出导入继续使用的途径，并要求直接移动本机资源前退出所有使用者；重新打开须先通过既有恢复与资源验证，不能视为自动修复。

R2.9–2.10 的完整版本可见性在支持环境中保留；不必把实现术语 W1、dirfd、SQLite hook 加入其 EARS 文字。R7.8–7.11 只适用于仍可验证的 `SOURCE_DIVERGED` canonical；设计须明确“配置根失效”不是普通 JSONL 内容 divergence，以免该组规则重新放行。

## 3. 平台 Requirements / Design 候选

定位：`.kiro/specs/windows-platform-enablement/requirements.md` 的 Requirement 5；共享合同由本 owner 拥有，而非新建 POSIX Spec。Windows R2/R3/R4/R8 不作范围收窄。

在相邻期望/Scope Lineage 明确本次修订为“POSIX TM 的支持环境和共享端口能力区分”，不能用平台名义把所有文件格式的外部替换保护一并撤销。

新增 R5.6–5.7 候选（当前最后编号为 5.5）：

6. When 消费者要求确认资源仍位于本次已授权的配置位置, the 共享平台边界 shall 同时区分原目录对象仍有效与配置位置仍指向该对象；已可观察的位置漂移 shall 拒绝，不能把重新打开同路径副本解释为旧授权的延续。
7. While POSIX TM 使用稳定本机命名空间与 LocalCAT 协作式资源生命周期, the 共享平台边界 shall 保持应用自行执行的命名操作受已持有的目录授权约束；该保证不得被描述为普通路径数据库驱动的全部内部访问已绑定目录句柄，也不得降低 Windows 专用能力的既有要求。

平台 D 最小插入位置：

- “POSIX Adapter”：定义从已确定稳定祖先/命名链到最初 root 的配置位置关联；保留初次 root 的 identity，后续只比较，不通过 fresh `bind_root()` 替代旧授权。
- shared contract/composition：通用 backend 只代表 rooted/lock/publish 能力，Windows private recovery 必须通过显式能力/owner 路线判定；`backend is not None` 不是 Windows recovery 证明。
- publisher 与 mutable reservation 段：为同 root、同 W1、已关闭 SQLite 并同步的既有 stage 提供窄的一次性交接，保持 POSIX sealed stage 的同对象发布；不能由任意 Path 收养文件。单次消费、close、失败归属由明确 owner 承担，不扩大到通用文件收养。
- 为既有 `ExistingFileMutationGuard` 补齐POSIX实现，只保留对象及原位置观察。复证不得在活SQL期间打开/关闭同inode临时句柄。DB命名pending在既有DB_REPLACED durable phase后完成terminal，关闭前与guard作同时存活的identity交接；后续SQL在guard中执行，pending自身内容不变式不改。平台Design的PendingPublication段明确此POSIX TM子阶段适用ADR-033，Windows和其他消费者不变。
- 对 root/parent/source/destination 末次检查保留非 CAS 语义；应用的 rooted 命名和 SQLite 检查间内部竞争分别陈述。

候选设计正文：

> 原 root 的 live identity 与配置位置关联是两个独立事实。平台保留本次最初取得的 root 与从稳定祖先到该 root 的命名关联，在消费者要求复证时重新核对当前名称链。原目录被移走而配置路径指向同字节副本时，旧 root 仍有效不构成位置有效；调用方不得重新绑定副本来刷新本次授权。检查为操作边界上的内部检查，不连续冻结外部命名空间，也不构成任意竞争的原子 CAS。

## 4. Core Design 接线与任务定位

定位：Core D “Platform composition”“SQLiteTMStore/连接配置/激活协调”“TMMigrationService”“Canonical authority 与 JSONL snapshot binding”的 mutation-proof 段及“集成测试”。具体函数接线由主稿代码发现表细化，不另建第二套 activation 状态机。

可直接加入的设计正文：

> POSIX activation 与 recovery 使用既有 POSIX v2 codec、coordinator phase machine 和 stock `sqlite3`；通用 backend 的存在不能选择 Windows v3 private-proof 路线。一次资源操作由原 owner 连续持有 root、W1、source pending publication、stage reservation 与发布上下文，子阶段只取得受限借用，不通过关闭 backend、释放 W1 再重入或重绑配置路径产生新授权。

> 资源家族上下文覆盖 source、stage、canonical、manifest、activation/schema/snapshot recovery 文件及相关失败收尾。至少在获得 W1 后、首次可能修改前、新 SQLite 连接或事务入口、SQL 阶段结束后、应用最终发布与业务成功闭合前复证本次 root 的配置位置关联。各阶段只增加其需要的检查；检查不等于 SQLite 内部每次 I/O 都使用 retained root，也不消除检查间外部竞争。读取连接可能触发 journal recovery，按真实连接生命周期处理。

> 已存在的数据库与 reservation 打开保持禁止隐式新建；fresh stage 先由 owner 排他创建。真实连接、cursor、transaction 和原始 FD 的生命周期属于同资源家族协调范围；资源替换前排空受影响 lease/连接，连接结束与失败收尾完成前不释放原 owner。StageSealer、active receipt、schema copy/backup、cold recovery 的重开同样消费该上下文，不能只改中央连接 helper。

> POSIX sealed stage 只有在全部构建连接已结束、journal 收尾与同步成立、内容和对象仍匹配 seal 后才能被一次性交接到 rooted publisher；发布保持已封存对象的身份。Windows portable candidate copy 保留原语义，不拿复制后的新 inode 冒充 POSIX 的原 stage。应用自行执行的 replace、rename、unlink、manifest/journal/backup/lineage/cleanup 均使用原上下文，不在失败路径重新解析配置路径以寻找可删对象。

> DB publication先完成原DB_REPLACED durable journal；在pending readback仍live时取得原root/W1下的mutation guard并比较双方live identity，随后完成pending的命名terminal及close，二者均成功才执行receipt SQL；coordinator仍ACTIVATING，命名terminal不是资源成功。SQL关闭后复证并采集final内容，再按原manifest/generation phase继续。交接失败保留实际命名与journal事实，冷恢复先核验完整closure；不改pending preliminary，不新增持久phase。详见PLAN §3.6。

> 检测到根位置或对象漂移时撤销该资源的新操作入口，既有 coordinator 保留恢复阶段与已观察事实。只有仍可证明的 prior/current 才能继续提供服务；不能证明时保持 resource-local unavailable/recovery-required，禁止自动 legacy fallback、删除未知日志或接管同路径副本。其他资源与项目由各自 owner 继续。冷启动重新核验资源与原恢复事实，不将运行时 inode 锚点持久化为跨重启授权，不把“重开”当作恢复成功。

以下是任务责任分组；最终编号与前置切片统一采用 [任务图](tasks-and-validation.md) 的 Core Task 10，不另追加早期讨论的T9.9，不修改已完成 5.x/9.x 勾选或历史 Implementation Notes：

- 分派与资源家族生命周期：覆盖 `tm_migration.py`、`tm_sqlite_store.py`、`tm_stage_sealer.py`、`tm_activation_*.py`、`tm_schema_upgrade.py`、`tm_snapshot_*.py` 的本次实际调用接缝；不作顺带模块拆分。
- 真实连接/stage/发布/收尾贯通：实现前必须有逐入口连接与阶段清单，包括 connect/backup/reopen，不以一个 helper 命中代替覆盖；正确关闭、同对象封存交接、恢复事实及终态均由各既有 owner 负责。
- 正常/协作/恢复与可见漂移验证：使用本文件第 7 节矩阵，区分首次硬条件与活事务外部替换边界实验。

平台可在既有 POSIX adapter 任务后追加小步项；不改写 Windows 0.5.2 Task 10.1 发行裁决。实际新编号由主稿统一。

## 5. ResourcePortability Requirements / Design 候选

定位：`.kiro/specs/language-resource-portability/requirements.md`。

- **R7.7 替换**：If source、destination、资源生命周期或基线在 preview 后、首次本地 mutation 前发生可观察变化, the apply shall 以 stable stale code 拒绝；If 在途操作随后发现位置或身份失效, the apply shall 停止受影响资源的后续操作并交由其 owner 处理恢复，不重新绑定同路径副本继续原计划。
- **R8.6 替换**：If profile validation、stage、publication、registry switch、runtime reload 或 cold reopen 任一步失败且 prior resource 仍可验证, the apply shall 保留或恢复 prior resource、registry 与 runtime；If 资源位置、身份或发布结果无法证明, the apply shall 返回 recovery-required、保留可确认的恢复事实且不报成功，不覆盖或删除未知资产。
- **R8.7 替换**：When apply 成功时, the service shall 从配置位置所对应的 final local resource 经业务 owner 冷重开，核对 payload semantic digest、record/profile counts、新 local authority 与 registry 可见性；未能证明这些事实 shall 不签发成功结果。
- **R12.3 替换**：The fault matrix shall 覆盖操作边界前已可观察的 source/destination parent replacement、应用发布期间的命名失效、hardlink/symlink/special file、manifest/member/CRC/digest/count/profile 篡改、preview 后 source/destination 变化及 stage/fsync/replace/readback/receipt-ledger/cleanup/recovery 故障；活动 TM 事务内部的外部目录替换 shall 另列为不受支持的边界实验，记录实际副作用与检测后的停止行为，不将全时序零副作用作为完成条件。
- **R12.4 替换**：When 验收支持环境中的失败路径时, the tests shall 证明旧导出文件、prior TM generation/store、prior termbase bytes、registry/runtime 与 source package 的各自保留事实；When 检验外部在线替换边界时, the tests shall 分别记录已确认保留、已提交、不可达或结果不明的事实，验证已发现失效后不继续接管或报告未经证明的成功，不推定所有 prior 字节未变。

R8.3 的首次 mutation 前 exact preview 复证、R8.4 的只经 TM owner、R9.4 的无成功 receipt、R9.6 的冷重开证明、R11.1–3 的 sync/不可传 live 数据边界、R12.7 的真实 NO-GO 全部保留。无需重写这些条文；R12.7 评估获准修订后的矩阵，不倒灌历史结果。

D 最小定位：“Import / Apply”与三个子节、“Publication Ordering”、“TM Profile”、“Publication / Recovery”。补充 owner callback 与 registry/runtime/receipt 终态仍在原资源 owner 上下文内；外部漂移分类不得用“返回 failure”掩盖已错误发布；不能让 resource 层直接开 SQLite。最终使用任务图 Resource Task 6 承载 POSIX create/replace 与上述 callback/recovery 接线、恶意输入回归，不把 Sync 前提直接改成已完成。

R6 的导出 target rooted 发布与保留规则不因 SQLite 支持范围收窄而删除；term CSV/v1 发布同样不受这个范围豁免。

## 6. UI 最小修订及不需重写的相邻规格

定位：`.kiro/specs/feature5-ui-integration/requirements.md`。

- **R5.6 替换**：If 首次激活失败, the LocalCAT Qt 编辑器 shall 不发布部分 canonical 状态；只有 Core 确认原 legacy 资源仍可安全使用时才继续提供原 legacy exact-only 能力，否则显示该资源不可用及恢复要求，不自动回落或接管同路径副本。
- **R5.9 替换**：If 已激活资源的外部来源后来发生分歧或显式更新失败且 last-known-good canonical 仍可验证, the LocalCAT Qt 编辑器 shall 保留该 canonical 状态并显示分歧或失败；If Core 无法证明该资源可用, the LocalCAT Qt 编辑器 shall 按资源不可用状态停止使用，不静默回落 legacy 查询。
- **新增 R6.14**：If Core 报告活动资源的位置或身份失效, the LocalCAT Qt 编辑器 shall 显示受影响资源和安全恢复要求，停止对该资源发起新操作，保持其他健康资源及不冲突的项目编辑和保存可用，且不得将目录复证呈现为用户可授予资源能力的新验证按钮。

D 定位：“Canonical 激活与更新”“TMResourceResolver”“Error Handling”。只消费现有 frozen 状态/安全 code，明确根失效不等于普通 source divergence，不进入 ADR-016 device-only re-attestation，也不进入 Fuzzy Gate。最终使用任务图 UI Task 10，复用现有 7.4 失败矩阵，验证失效后新查询/写回/重复激活拒绝、其他资源继续、项目编辑/保存、晚到完成不能恢复旧状态；不预先要求新 DTO/enum。

Sync R3.1–3.4、D“Immutable artifact/相邻合同与执行依赖/owner_apply_started 恢复”已有一致包规则；复验资源 adapter 返回同一 validate/preview/apply，不新增第二条 snapshot 通路。“批准快照”只有经过现有 owner 明确认可的 artifact 才可消费，不能借此允许 raw SQLite 家族。RPY T6.2 的双设备包验证与 T6.3 的 Sync 前提仍是完成出口。

## 7. 最小验证清单与已有落点

下表是测试计划，全部未在本轮执行。既有测试名只是复用位置，不代表当前 POSIX ResourcePackage 路径已通过。

| 验证组 | 真实通过条件 | 现有落点与需补缺口 |
| --- | --- | --- |
| POSIX 正常 create/replace | Application→ResourcePortability→TM owner 真正完成新建和已有 generation 替换；源包不变；resource/registry/receipt 一致；独立重开和真实 query/append/export 可用 | `tests/test_resource_portability_exports.py::test_tm_package_create_and_replace_use_core_generation_transactions`；未提交 `tests/test_tm_resource_snapshot_posix.py` 的 create/replace/callback/cold 用例。需要实际 Mac/Linux source 环境，不能仅 fake backend |
| 配置源内容分歧 | JSONL／manifest外部内容变化仍走SOURCE_DIVERGED；canonical位置及内容有效时保留原Lookup/Update，不被目录替换边界误拒绝 | 原Core R2.13、R7.8–7.11及source divergence测试继续覆盖 |
| 同资源连接/lease 与写回 | query 与 append 各只使用完整 generation；replacement 等待已有 lease，拒绝新 lease；同资源第二进程不能并行获得原 W1；结束额外原始 FD 不提前释放相关 SQLite 锁 | `test_tm_sqlite_store.py::test_canonical_revision_concurrent_append_is_one_complete_snapshot`、`test_local_append_preserves_variants_and_raw_exact_winner`；`test_tm_explicit_import_rebuild.py::test_import_still_drains_an_existing_operation_lease`；`test_platform_fs_posix.py::test_flock_is_exclusive_across_processes`。补真实进程/连接组合，不以 W1 单测代替 SQLite 生命周期 |
| stage 同对象交接 | creator reservation、seal、两次 Gate B、发布/close 持续对应同一 root 与对象；失败不收养任意 Path，不删除 foreign inode | `test_tm_stage_sealer.py`；`test_tm_activation_publication.py::test_first_activation_publishes_one_complete_generation`、`test_existing_canonical_switches_from_prior_to_new_generation`；`test_tm_explicit_import_rebuild.py::test_seal_failure_hostile_stage_swap_never_unlinks_foreign_path` |
| 正常中断恢复 | PREPARED、DB_REPLACED、MANIFEST_PUBLISHED、GENERATION_PUBLISHED 的受影响 phase 可完整旧/新或 recovery-only；重新启动不混用 DB/manifest，重复恢复不增 generation | `test_tm_activation_recovery.py`、`test_tm_activation_rollback.py`、`test_tm_activation_publication.py`；schema 改动链实际受影响时复用 `test_tm_schema_upgrade.py` 的 copy/rollback/cold-recovery 用例。追加进程终止只选变化接缝，不重造全量矩阵 |
| snapshot/export/cold reopen | append 后导出是完整 read snapshot；refresh 与 receipt/recovery 相符，final owner 冷重开 | `test_tm_snapshot_refresh.py::test_refresh_pins_stable_snapshot_under_concurrent_append`、`test_concurrent_refreshes_serialize_and_never_poison_divergence`；`test_tm_snapshot_recovery.py`；`test_resource_portability_recovery.py::test_cold_service_completes_owner_published_create_after_registry_fault` |
| 已可见 root 替换：真实 builder | 在 `_build_stage` 入口改名原 parent 并复制同字节新 parent，然后继续真实 builder；拒绝连接/建 stage/发布到副本，原授权未被重绑，owner callback/registry/success receipt 不发生 | 未提交 `test_tm_resource_snapshot_posix.py::test_same_byte_parent_replacement_cannot_redirect_real_builder` 是关键反例；须重新运行实际新实现。前一个 `test_parent_replacement_fails_closed_without_writing_new_parent` 在改名后立即抛异常，仅覆盖失败收尾，不能替代真实 continuation |
| 已可见 root 替换：后续阶段 | 首次 mutation 前、真实 SQLite reconnect 前、应用 publication 前及终态检查出现持续可见漂移均拒绝；再次请求不接管副本 | 在上述 owner tests 与 `test_platform_fs_posix.py` 新增 root 本身（不仅 root 下 ancestor）的 rename/clone 用例；构造 same bytes/different inode，检查两边实际文件、journal/receipt 与回调 |
| 恶意输入不退化 | 包路径/链接/摘要/计数/closed grammar、stale source/destination、foreign artifact 均拒绝；没有因为外部替换不支持而跳过 | `test_resource_portability_exports.py::test_preview_rejects_source_or_destination_inode_replacement_before_mutation`；`test_tm_replace_preview_rejects_canonical_revision_advance`；`test_resource_portability_architecture.py`、ResourcePackage carrier/hostile 测试；受影响 platform no-follow/link 用例 |
| 资源级 UI 停止 | 失效资源不被下一查询、刷新、写回或晚到 activation completion 恢复；其他 TM/术语及项目编辑保存继续 | `test_tm_resource_lifecycle.py::test_missing_path_is_local_unavailable_and_other_legacy_survives`、`test_present_but_ambiguous_activation_facts_never_fall_back_legacy`；`test_editor_controller_inactive_tm_activation.py`、`test_editor_controller_tm_activation_completion.py`、`test_qt_resource_portability.py` |
| 活事务外部替换：残余边界 | 在独立进程制造 journal 创建/INSERT 后替换，记录原/新主库、日志、commit/rollback/close 结果；检测后停新操作、不假成功。不要求实验无副作用，不将其标为产品正常路径 PASS | 保留 `review/posix-sqlite-binding/reproduce_sqlite_namespace.py` 和旧反例；新增时明确环境和原始观察，不改旧结果。不是首发无损硬门，也不自动立项 VFS |
| 交付与离线迁移 | Windows 普通 frozen 与 Mac/Linux source 通过完整 ResourcePackage 交换、导入后查询写回和冷重开；包与本机资源身份分离，资格不搬运；退出后重新定位也必须验证完整家族 | 既有 RPY T6.2/T6.3、`tests/test_rpy_manual_package_journey.py` 候选和上述 owner tests。按实际共享 Core/平台变更选 Windows 原生回归，不以 POSIX 结果代替 Windows，不重签历史候选 |

### 不应成为本轮新工作

- 不新增 Fuzzy 类目录验证按钮、硬件/目录资格、持久 proof registry 或长期测评台账。
- 不因纯文档/hash 变化重跑 100k；实际检索、runtime、worker 或兼容性输入有变化时仍按既有 Core 规则判断重验范围。
- 不把正常 stage/publish/cleanup 错误解释成“外部不支持”；不通过删除旧断言或缩小测试输入使候选假通过。
- 不静默迁移 source/default 资源路径，不复制活 SQLite/journal 作为同步或迁移方案。

## 8. 主稿整合时应显式指出的剩余决定

1. 治理/ADR 的候选和 Git 整合方案由主稿提供；本文件不决定受保护历史的重排，也不把 `b3d5e26` 当开发回退点。
2. root 复证、stage 窄交接及所有真实 connection/reopen 的签名/生命周期必须在主稿接线表落到具体现有类型和函数。仅上述责任段落不足以直接开工。
3. 当前 POSIX create/replace 仍未贯通；已撤回候选与旧实验保留其原状态。实施完成后的 GO 依赖本轮实际结果，不依赖这份文字或曾有 happy-path 成功。
