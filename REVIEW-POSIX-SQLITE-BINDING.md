# POSIX TM 目录绑定：保护范围与最小修复架构评审

日期：2026-10-12。交接基点：`temp-posix-sqlite-binding-review@3dbf2ed920699db22f3241287860563b78950de2`；生产源码基线：`2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5`。

本文续接 [HANDOFF-POSIX-SQLITE-BINDING.md](HANDOFF-POSIX-SQLITE-BINDING.md)。只给出审阅、产品边界建议和后续验证条件，不批准或实施修复，不修改正式 R/D/T、ADR、任务状态或历史验收。本轮未运行 LocalCAT 产品、未应用已撤回补丁、未构建 VFS 或 frozen；另在隔离 Linux 环境运行了六个合成 SQLite 用例，范围见第 2 节。源码结论、原交接观察、本轮观察和待验证建议分别说明。

## 1. 结论与优先级

**首选不是新建 VFS，而是先明确“稳定本机命名空间＋协作式资源生命周期”的支持边界，再修复 POSIX 分派、原目录与配置路径的关联、既有 stage/activation/recovery 接线。** 这是有条件的最小路线：它不满足“活连接期间任意外部目录替换仍绝不触及替换目录、原库与日志始终无损”的强承诺。若 owner 坚持后一个承诺，不能把本方案算作达标，才需要评估更深的 SQLite I/O 绑定或不同存储架构。

| 优先级 | 裁决建议 | 不应扩大的范围 |
| --- | --- | --- |
| P0 | 正常 Mac/Linux TM ResourcePackage create/replace 不能因 POSIX 被误分派到 Windows proof 路线而拒绝；修复前仍不得宣称跨平台资源交换已完成 | 不要求 Mac/Linux frozen，不复活 W3 |
| P0 | 保留 LocalCAT 自己协调的并发、完整 generation、同资源替换、崩溃恢复和来源/registry 一致性；已可观察的目录漂移不得被当成新授权 | 不能以“外部竞争不支持”豁免自身漏锁、重绑错误、盲目 cleanup 或虚假成功 |
| P1 | 对运行中外部移动/替换目录明确不支持；在约定边界发现漂移后阻断该资源、保留恢复事实，要求停止外部变更并重新核验 | 不承诺所有瞬间都能检测，也不承诺检测前 SQLite 内部无写入或无日志副作用 |
| 条件备选 | 只有额外要求覆盖活事务中父目录替换，才进入一个有限 VFS 可行性实验 | 不先建完整平台层，不以注册成功或几行 wrapper 宣称解决 |

这不是用“风险低”否认反例。本材料没有事故频率或用户行为统计；推荐依据是触发条件是否属于产品正常工作流、可能损失，以及保证的工程代价。普通导入不会主动把整个资源目录改名并在原名创建克隆；而程序自己打开错误目录、失去原 W1 后继续发布，属于可以且应当修复的集成问题。[H][M][RC]

## 2. 已确认的事实与证据限度

### 2.1 数据位置不是“项目目录”这一种

[ResourceRepository][RR] 将托管资源放在 `config_dir/resources`，registry 是 `config_dir/resources.json`。正常入口的 [qt_editor.py][QT] 传入应用数据目录；默认 TM 参数在普通 frozen 下为 `data_dir/tm.jsonl`，source 下为源码根的 `tm.jsonl`。`TMResourceSnapshotPort` 从每个 `ResourceConfig.path` 创建 `CanonicalResourceIdentity` 并调用 Core；初始 reservation 绑定的是 `identity.canonical_sidecar_path.parent`。[RP][M]

因此，保护对象是**被登记的应用级 TM 资源家族**，包括相邻 sidecar 和恢复文件。不能把源码目录说成所有 TM 的位置，也不能把应用数据目录说成与 SQLite 无关。默认采用稳定托管目录可以减少误触，但不能无授权把已有外部/default TM 迁走，或改变 sidecar 邻接合同。

### 2.2 三个不同问题

**分派错误，静态确认。** `tm_migration._recover_existing_initial_activation()` 用 `reservation._backend is not None` 进入 portable initial recovery；POSIX 调用也携带 backend。通用 backend 的存在不能证明 Windows private-proof/recovery 可用。当前该导入路径仍拒绝，已撤回补丁不是部署现状。不能简单把 backend 改成 `None`，否则丢失上游持有的 root/W1/bound family。[M][H]

**配置名称与 retained root 脱节，静态与历史观察吻合。** `platform_fs_posix._bind_root()` 对给定 root 打开 FD，构造的首项名称是 `None`；`_reprove_directory_chain()` 对首项只核对 `fstat`，没有把原配置名称重新关联到这个 root。根目录改名后，该 FD 仍可有效，而旧路径可能已指向新目录。与此同时 `_open_configured_connection()` 仍将 `Path` 或普通 URI 交给 `sqlite3.connect()`。这解释了为什么“旧 reservation 复证通过”与“builder 打开替换目录”可以同时发生。[P][S]

历史候选在 `_build_stage` 前置换目录、继续真实 builder，观察到新目录另取 W1、建立 stage、发布 generation 1 并返回成功；原目录 canonical 不变。原运行是 `07a12b7` 加当时未提交候选，不是当前 `2e1cbce` 的运行结果。原始结果和完整候选仍可追溯，本轮未重新部署或重跑这个产品候选。[RC][RCP]

**SQLite 内部主库/日志分离，不是上一问题的同义词。** 交接中的 Mac 实验显示 INSERT 后、commit 前替换父目录，可能出现原主库改变、原 journal 遗留、克隆 journal 被删除，而 commit 返回成功。`SQLITE_READONLY_ROLLBACK` 表示该只读打开需要恢复，不能仅凭它断言数据库已经不可修复地损坏。也不能由此推导 Windows、其他格式或全部 SQLite 版本都有相同结果。[O][H][E]

### 2.3 本轮独立合成复核

本轮在 Linux x86_64、Python 3.13.5、SQLite 3.46.1、kernel 6.18.44 的隔离临时目录，运行交接的 [reproduce_sqlite_namespace.py][REPRO]。本地脚本按 Git blob 计算核对为 `b65c61bf02f7ae77fb63d08fd3e17cdc0082f48b`，与分享件一致。三个原脚本用例各一次，结果如下；这不是 Mac 3.14.7/3.53.1 的新验收。

| 用例 | commit 返回 | 原目录主库 | 替换目录主库 | close 后 journal | 原库只读重开 |
| --- | --- | --- | --- | --- | --- |
| 无替换对照 | 成功 | 写入新行 | 同一目录，不另计 | 无遗留 | seed＋transaction |
| connect 前替换 | 成功 | 未改变 | 写入新行 | 两边均无遗留 | 仅 seed |
| INSERT 后替换 | 成功 | 已改变 | 未改变 | 原目录有、替换目录无 | `SQLITE_READONLY_ROLLBACK` |

有一个需要控制的因素：原脚本在持有 SQLite 连接的同一进程内 `shutil.copytree()`，读取并关闭原主库的额外 FD；SQLite 官方指出同进程额外 close 可影响传统 POSIX advisory locks。[C] 因此本轮再做三例有限对照，仅将原脚本的两句 `configured.rename(held); shutil.copytree(held, configured)` 替换为独立进程执行：

```python
subprocess.run([
    sys.executable, "-c",
    "import os,shutil,sys; os.rename(sys.argv[1],sys.argv[2]); shutil.copytree(sys.argv[2],sys.argv[1])",
    str(configured), str(held),
], check=True)
```

其他事务与观察逻辑不变；独立进程不调用 SQLite。第二组三例全部观察字段与第一组相同，两个命令退出码均为 0。**在这个环境，目录/日志分离不依赖同进程复制导致的额外 close。** 这消除一个实验混杂因素，不证明产品锁实现、磁盘耐久、其他平台或所有时序。未执行原库可写恢复，不把可能的回滚结果写成实测；未测性能，不运行 100k Gate。

## 3. 推荐支持范围：哪些应阻塞交付

| 场景 | 推荐保证与失败处理 | 用户损失、触发条件与成本判断 | 是否阻塞本轮交付 |
| --- | --- | --- | --- |
| 稳定本机数据目录正常使用 | Mac/Linux source 完整导入、激活、查询、写回、导出和冷重开；保留校验、来源、顺序与完整事务 | 当前误拒绝直接阻断正常跨平台交换；已有 owner 可复用 | 是，不能只靠文档宣布完成 |
| LocalCAT 自己协调的并发、DB 替换、崩溃恢复 | 所有参与者遵守同一资源家族协议；替换前排空相关连接/lease，旧或新完整版本可判定；不把旧主库与新日志混用 | 正常更新、重试、双开和中断就可能触发，损失是合法资产丢失或半发布 | 是，保留为硬要求 |
| 退出/释放所有资源使用者后的离线迁移 | 首选导出 ResourcePackage/逻辑快照，在目标机器导入并取得本地身份；同机重定位必须重新绑定并核验完整家族 | 正常用户需求；复制活库不是迁移。发生崩溃时“进程不在”也不等于没有 hot journal/pending activation | 是，但不要求新增任意 raw-sidecar 搬运工具 |
| 运行期间用户/备份或同步工具改名、恢复或替换资源目录 | 明确不支持在线替换。检查点前已存在且持续可见的漂移须拒绝；在途检测后停止新操作、拒绝将未闭合状态宣布成功并给出恢复指引 | 可能是误操作而非攻击；晚发现仍可能丢失近期 TM 写回或留下需恢复日志。检测成本可控，保证任意时刻无副作用则需更深 I/O 改造 | 阻塞“已可见漂移仍继续/虚假成功”；不把全时序零副作用作为默认首发门 |
| 任意非协作同用户进程持续制造竞争、改写文件或 ABA | 不提供全能抗篡改/零损坏保证；保留可证明的拒绝与操作范围限制 | 同 UID 可以直接改写资源。单靠 VFS、0700 或 W1 都不是独立安全隔离边界；强承诺会无限扩张 | 不应成为本轮交付前置 |

建议面向用户的说明是：

> 翻译记忆库使用期间，请勿从文件管理器或同步/恢复工具移动、替换其目录或数据库文件。需要迁移或恢复时，先关闭所有使用该资源的 LocalCAT 进程，优先使用资源包导出/导入。发现位置变化后，LocalCAT 会停止该资源的后续操作并要求重新核验；不会把同字节副本直接当成原活动资源。已发生的外部在线替换可能需要从一致备份恢复，重新打开并不等于自动修复。

“稳定”是可理解的运行前提，不要求每种磁盘型号再做认证。应支持的具体本地文件系统仍按现有平台能力和实际验收确定；不顺带扩张网络盘、同步目录在线恢复或 Mac/Linux frozen。目录私有化有助于减少误触/其他用户访问，不能宣称挡住同 UID 的非合作程序。

**范围收窄不适用于恶意包输入。** 路径穿越、symlink/hardlink/special file、包成员/摘要/计数篡改和明确的 preview stale 继续拒绝；这些是应用在正常导入中就必须处理的输入边界。[RPR]

## 4. 首选最小修复：保持现有 SQLite 驱动，补齐具体接缝

这条路线的前提是 owner 批准第 3 节的支持范围。它是比新驱动/VFS 小的修复，不承诺两行分派修改即可完成。

### 4.1 修分派，而不是关闭平台能力

将“使用通用 backend”与“使用 Windows portable activation/recovery codec”分开。由现有 composition/owner 边界显式选择 POSIX 实现和 Windows 实现；Windows proof 不满足时仍拒绝 Windows 操作，不能通过删除字段把 v3 伪装成通用恢复。

POSIX 的 root、W1、source pending publication、stage reservation 应被原业务 owner 连续持有并借用至必要的失败收尾。不要把 `_backend=None` 当作选择 POSIX 的开关，也不要先放掉 W1 再走 legacy 重入。原候选同时改了恢复、publication 名称、清理和 reservation 生命周期，不能作为已正确补丁整块恢复。[M][RCP]

### 4.2 补原 root 与配置位置关联，不能每阶段重新授权

平台侧为资源操作建立**配置位置关联检查**：从已确定的稳定祖先/命名链核对当前配置 root 与本次 retained root 是同一对象；Core 只消费 opaque 成功/拒绝，不读取私有 FD。只核对 `fstat(rootfd)`、或只核对 root 下面的 basename，均不足以发现根名称本身被替换。[P]

至少在取得 W1 后、首次可能写入前、SQLite 新连接/事务入口、SQL 阶段后及应用自己最终 publication/registry success 前进行适合该阶段的复证。source、stage、canonical、manifest、backup 和失败清理消费同一资源家族上下文；重新按原路径 `bind_root()` 得到一个新 root，不能替代对旧绑定的比较。跨阶段出现同字节新 inode 也不能刷新旧授权。

这些检查是**持久可观察漂移的拒绝点**，不是连续监控，更不是 SQLite 每个 syscall 的对象绑定。最后一次检查与后续系统调用之间仍有窗口，ABA 也可能逃过检查。它们足以针对“在 `_build_stage` 入口已完成替换、随后继续真实 builder”的既有测试增加正确的拒绝位置；仍要在实际候选重放确认，不能据静态设计先写 PASS。

### 4.3 SQLite 生命周期与应用命名操作分别约束

既有 `_open_configured_connection()` 保留短连接、DELETE/FULL/FK 和 busy 策略。已有 reservation/canonical 的打开保持 `mode=rw`，避免缺失时悄悄创建空库；fresh stage 先由 owner 排他创建。SELECT 也可能遇到 hot-journal 恢复，不能把“读 SQL”当作底层绝不写文件。[S][L]

应用自己的 replace/rename/unlink、activation journal、manifest、backup、lineage 和 cleanup 继续走保留目录下的命名与对象校验；不要仅在返回时加一个错误就容许这些操作落入替换目录。优先复用 POSIX v2 的 journal codec 和 coordinator phase machine，改其 I/O 接缝，不另建一套业务激活流水线。

现有 POSIX 激活要求最终 DB/manifest 仍为 sealed stage 的对象；[activation manifest 发布][AR] 明确比较发布前后 identity/digest。既有 portable live authority 的 `copy_asset_to_new_candidate()` 会创建新文件，不能直接拿来满足同 inode 合同。[SEAL] 可先评估为**现有已持有、已关闭 SQLite、已同步的 stage**增加窄的交接/发布能力，复用原 publisher，而非复制文件再声称还是原 stage。该能力必须拒绝调用者凭任意 Path 收养文件，且把 journal 收尾、单次消费、失败归属说清。这里不需要先规定新的持久 schema。

不仅检查中央 connect helper：Sealer、schema backup、active receipt、cold recovery 等重开均必须按其真实阶段复核。交接清点了多个直连点，本轮未声称完成全库逐点覆盖；实现前应针对本次真实调用链补齐，不能以“改了一个 helper”作为完整证明。[D]

### 4.4 保证协作并发，而不是用锁替代命名绑定

同资源的 SQLite 工作、应用备份/哈希 FD 和替换操作必须服从 coordinator 的 drain/reservation 顺序；若两个 LocalCAT 进程可以同时接触同一资源，需证明 W1/生命周期协议在两端一致，第二 owner 正确等待或拒绝，不能让它绕过正在替换的家族。SQLite 自身文件锁不能单独协调“先关闭再替换 inode”的应用流程。[CORE]

POSIX 同进程直接打开、读取、关闭主库的额外 FD 是独立审查项：不要在 SQLite 连接仍持锁时随意做原始文件 hash/复制。优先在同 inode 的相关 SQL 连接/statement/backup 结束后的阶段计算文件证明；必要时用 SQLite backup 取得一致副本，再验证已关闭的副本。需要核对的是活句柄和关闭顺序，不只是“上层事务已经 commit”。这是一项潜在正常并发风险，本轮未证明 LocalCAT 已发生由此导致的产品损坏。[C][B][CL]

若维持多进程协作的接线明显大于当前需求，可把“同一 TM 同时只由一个 LocalCAT 进程持有，第二进程报告资源忙”作为**另行明确的小方案**，复用资源级 lease；不能静默改变既有并发合同，也不能把 `locking_mode=EXCLUSIVE` 误当目录防改名。

### 4.5 漂移后的拒绝与恢复不能变成自动接管

发现漂移时锁存本次资源会话失效，停止发新 query/write/import，撤销尚未提交的可见结果；不得重新绑定同路径克隆、继续签发新 generation、成功 callback 或成功 receipt。保留既有 source package、可证明的恢复物和失败范围，其他独立资源/编辑能力不应因该资源失效被全局撤销；项目保存成功与 TM 写回失败须分别表示。

在 SQLite 已开始写事务后才发现外部替换，**不能承诺只要不 commit 就没有写入，也不能承诺 `rollback()/close()` 没有文件访问**。SQLite 可在事务中写页，最终关闭可能回滚；原文件与日志已经分离时，清理还可能触及命名路径。[PR][CL] 因此应让 owner 结束其连接生命周期，但停止额外的应用层盲目回滚、递归清理和自动覆盖；未证实的资产状态如实为 unknown/recovery-required，而非 unchanged。此路线不解决该时序中的所有 SQLite 内部副作用，不能包装成旧强保证的等价实现。

“要求重新打开”具体是：停止外部变更/恢复任务，保留数据库与日志家族，退出所有使用者，再经原 recovery owner 检查或导入一致快照；不是在异常后立即重试相同 pathname。冷恢复要区分正常 hot-journal/activation recovery 与目录身份已不明的情况，不能随意删除 `-journal`，也不能从另一份同字节目录推导原执行已经提交。持久 inode 不新增为跨重启授权；继续依赖当前根绑定及现有资源/lineage/receipt 规则，无法证明就请求人工恢复。

## 5. 较小备选能覆盖什么，不能覆盖什么

| 方案 | 能解决的范围 | 剩余窗口、后果和成本 | 结论 |
| --- | --- | --- | --- |
| 仅修 POSIX 分派 | 普通调用不再误入 Windows proof | 重现中的旧 root/新路径脱节、异常清理和恢复接线仍在 | 不能独立作为完成条件 |
| 分派＋同家族生命周期＋root 位置复证＋rooted 应用发布 | 正常使用/协作替换；已可观察漂移前拒绝；应用命名操作不主动改投克隆 | SQLite 内部检查间竞争仍在，须接受第 3 节边界 | 首选 |
| 将新资源默认保存在稳定托管目录，限制在线外部替换 | 降低用户把活动 TM 当普通交换文件覆盖的机会 | 同 UID 仍能改目录；历史 source/default TM 不能自动搬迁；位置本身不是安全证明 | 配套说明，非新存储架构 |
| 单 owner/资源、短连接、drain、SQLite backup | 改善 LocalCAT 自己的并发与备份一致性 | 不阻止外部目录改名；backup 输出的发布仍需 owner 协调 | 优先复用；必要时单 owner 作为明确备选 |
| 内存 fresh stage＋serialize | 让这一次新 stage 构建不访问待替换目录的 SQLite journal | canonical 正常写回、receipt 更新、hot recovery 仍走磁盘；全库副本及序列化增加内存/复制成本 | 可局部实验，不是全资源修复，不因它放宽 512 MiB |
| 私有磁盘 staging 工作区＋关闭后发布 | 隔离候选构建和不可信输入，减少对 final 的提前触碰 | 工作区自身仍需稳定；跨卷发布、same-inode seal、后续 canonical I/O 仍需解决 | 有实际缺口才选，不默认迁移全部数据布局 |
| `mode=rw`/NOFOLLOW/`DBMOVED`/`HAS_MOVED` | 分别辅助控制创建、符号链接或检测部分变化 | 都不是已绑定目录下全部主库/日志 I/O；`DBMOVED` 已有时序反例，stdlib 也未直接暴露所有 C 控制接口 | 辅助防护，非完整修复 |
| `fchdir`、`/dev/fd`、`/proc/self/fd` 或主库硬链接别名 | 个别平台可能打开某对象 | Mac 交接负例；SQLite 命名与 journal 配对不因此统一；CWD 是进程级状态，硬链接引入多名称风险 | 不作为跨平台方案 |
| WAL、PERSIST/TRUNCATE、exclusive locking、关 journal | 部分模式改变日志删除/保留或锁行为 | WAL 新增 WAL/SHM 与恢复面；PERSIST/TRUNCATE 不固定后续打开的目录；关 journal/锁牺牲可靠性 | 不作为为过反例而切换的参数修复 |
| 默认 unix VFS 的 `xSetSystemCall`/全局 syscall hook | 理论上可尝试重定向底层调用 | 可选测试接口、作用范围与版本耦合、线程/其他连接污染风险；不是安全的单连接 dirfd 注入 | 不把它叫作无 VFS 成本的捷径 |

SQLite URI、VFS、file-control、pragma 与 backup 的接口依据见 [U][V][VC][FC][PR][B]；Mac FD/fchdir 观察来自原交接而非本轮重测。[H] 以上不是要求遍历所有选项；首选失败时只调查导致失败的那个具体假设。

## 6. 精确的合同调整建议

**必须先获批，再改实现和验收预期。** 推荐部分限定旧承诺，而非删除用户数据保护。当前拒绝路径、原候选失败、原 SQLite 负证据均保留原状态。

| Owner / 位置 | 建议的精确修改 | 明确保留 |
| --- | --- | --- |
| ADR-020 决策 2/8/12 及对应支持边界 | 由新的明确裁决限定 POSIX SQLite：应用自己的文件命名仍用 rooted authority；stock SQLite 在已验证但须保持稳定的本机命名空间中工作；不从 retained root FD 推导其内部 I/O 已绑定该 FD。第 8 条非 CAS 边界不应被后续测试隐式扩成抗任意同用户竞争 | 平台/业务 owner 分权、live identity、opaque 接口、来源/包验证与 Windows 现行保护，不修改旧正文伪装原已满足 |
| Core R2.4、2.9–2.12，R7.13–7.14 | 保留稳定目录和协作操作下的原始资产、完整 generation 与回退保证；补充外部命名漂移的异常前提：无法证明 prior/current 时资源级 fail-stop，不强行提供可疑 canonical/JSONL，也不自称所有字节未变 | 不能把自己制造的错目录、漏锁、正常崩溃恢复缺陷归入外部不支持 |
| Core Design：Platform composition、SQLiteTMStore、TMMigrationService、activation/recovery 的 mutation-proof 段 | 写清平台分派、一个资源家族上下文、root 配置位置关联、所有相关 reopen 的阶段检查、SQLite/原始 FD 生命周期、sealed stage 同对象发布和漂移后拒绝。明确应用命名保证与 SQLite 内部竞争残余不同 | DELETE/FULL/FK、stage→seal→activation、现有 POSIX v2/Windows v3 的真实区分、同一业务 owner；不先写 VFS 必选 |
| ResourcePortability R7.4/7.7、8.3 | preview 继续绑定明确的 source/destination/baseline；首次 mutation 前重新核对。补充这不是整个 SQLite 活事务的名字冻结；发现漂移的下一操作边界不得继续接管替换目录 | 可观察 stale 的确定性拒绝、恶意包/路径输入检查 |
| ResourcePortability R8.4/8.6/8.7、R9.4/9.6 | 仍通过 Core 完整事务，success 必须对应真实 final resource/registry/receipt；外部漂移时如实区分 prior 已保留、发布已提交但不可达、或结果不明，复用现有失败表达；重新打开不是自动修复承诺 | 禁止 ResourcePackage 直接开 live SQLite；禁止仅返回 failure 却照常发布替换目录 |
| ResourcePortability R12.3/12.4/12.7 | 将“parent replacement”分成可观察边界前替换、应用自己命名操作期间替换、SQLite 内部活事务替换三类。前两类按明确能力检验拒绝/对象范围；第三类保留为支持边界负例和检测后行为测试，不再无条件要求 prior 所有字节不变。第 12.7 按获批后的真实矩阵判 NO-GO | 不跳过失败假装 PASS；原反例不能回写为原候选通过 |
| 用户说明与 source/default 资源入口 | 写清应用级位置、sidecar 邻接、关闭所有使用者后再迁移、优先逻辑资源包交换、在线恢复不支持；历史默认资源不静默改路径 | 不新增 Mac/Linux frozen，不禁止正常只读备份概念，不把不一致文件复制称为受支持备份 |

建议加入 owning Requirements 的核心表述：

> 在资源目录、数据库及 SQLite 辅助文件未被应用外部在线移动、替换或改写，且所有 LocalCAT 使用者遵守同一资源生命周期协议的支持环境中，TM 操作继续保证完整版本发布、失败保留/恢复和正确冷重开。每次操作在约定边界复核配置位置与已绑定资源；已发现漂移时拒绝后续操作、停止自动接管并给出恢复要求。对活连接期间外部替换造成的内部 I/O、已发生写入及日志配对损失不提供无损保证，也不将未经证明的结果报告为成功。

还需保留有限性：边界复证不能保证发现检查间 ABA；“最后一次终态核对时配置可达”不是“成功返回后用户再移动目录仍保持可达”。反过来，普通运行中的 final 冷重开失败仍是交付阻断，不能一律归因于外部行为。

## 7. 什么时候才值得 VFS，怎样有限验证

### 7.1 真正新增的收益

若 owner 明确要求：**即使父目录在 SQLite 打开主库之后被外部改名并在原名放入克隆，SQL 的主库、journal、hot-journal 探测和删除仍只能作用于同一个已绑定目录对象**，边界检查和普通路径连接不足。需要让每个相关 I/O 使用 retained directory authority；VFS 是可表达该能力的选择，不是唯一逻辑可能性。[V][VC]

VFS 仍不能防止同 UID 直接覆盖原主库，也不能自动恢复配置路径可达性；保留目录内部 basename 的非合作替换、stat→unlink 非原子窗口仍要明确。不能把 VFS 说成任意恶意进程下的无损保证。

### 7.2 路线比较

**stdlib sqlite3＋可信内部 C VFS**在接口层较小：可通过加载到宿主 SQLite 的扩展使用 `sqlite3_api_routines` 注册，保留现有 Python Connection/异常/SQL API。但这只是集成优势，不免除锁状态机、主库与日志命名、sync、异常清理和最终 xClose 的工作；当前普通连接关闭扩展加载，需明确的受控 bootstrap 例外。macOS Python 是否支持加载扩展取决于构建，不能假定每个 source 用户已有这个条件。[S][EXT][PY]

**APSW＋Python VFS**省去部分 C/Python 胶水，不提供现成的目录 authority 协议；驱动事务/异常/连接类型不同，官方二进制的 SQLite 与 stdlib 不应被假定为同一个实例。仅将某一写路径换 APSW，同时让 stdlib 在同进程继续访问同一个 inode，不是可接受的简化。[AP][AV]

两条路线都要处理多连接锁与 close 协调。同一个 SQLite 实例不等于自定义 VFS 自动接入默认 unix VFS 的私有锁表；OFD 探针只证明某环境的基础锁现象，不证明 SQLite 的 SHARED/RESERVED/PENDING/EXCLUSIVE 及恢复协议。[D][X] 如果底层错误在 commit/cleanup 中被忽略，还需由 retained owner 状态阻止应用宣称全部完成；不能假设回调抛错总会变成提交失败。[AV]

**source/frozen 影响：** Mac/Linux 新增 native 构建/加载与 ABI 维护，不是新增 frozen；预编译组件可以减少用户编译负担，但项目承担架构、系统版本及 Python/SQLite 兼容测试。Windows 不加载 POSIX 组件；共享 Core/连接适配变化仍按实际影响验收。驱动、索引行为、SQLite runtime、检索相关执行输入变更触发原兼容性及真实 Gate 规则，不能据“SQL 没改”直接复用全部资格，也不因纯文档或 Git 元数据变化重验。

### 7.3 仅在更强范围获批后执行的一个可行性实验

只选一条路线，不同时开发两套。优先验证保留 stdlib API 的路线是否真的可行，但不给它预先通过结论。合成 fixture 限于一个主库、DELETE/FULL、两连接/两进程、少量真实行；不接 UI、不建新平台框架、不先运行 100k。

实验须同时回答：宿主同一 SQLite 实例是否实际选中指定 VFS；主库/journal/xAccess/xDelete/目录同步是否始终消费同一目录；目录在连接前、journal 创建前、INSERT 后及 rollback/close 时变化会怎样；崩溃遗留 hot journal 能否在原家族重开；第二连接/进程是否正确互斥；关闭无关原始 FD、statement/backup 延迟完成、重复撤销是否破坏锁或过早释放 authority。观察原目录、替换目录和各自日志的实际对象/内容，而非只观察异常。

成功退出仅表示这组窄机制有真实证据，随后估算接入全部相关 reopen 的增量。出现任何普通 pathname 回落、需关闭 journal/锁才能成功、同 inode 驱动混用、无法解释清理/close，或必须改造大量 runtime 才能加载，就停止该路线并重新裁决；不能以注册成功续建完整系统。Mac 与 Linux 至少各有目标环境证据后，才讨论共同能力，单机 OFD 结果不足。

## 8. 最小验证清单与实施顺序

先批准支持范围及对应合同 delta，再在独立实现线修复；本评审分支只保存交接与讨论，不移动治理分支或改写开发历史。

| 组 | 最小实验/验收 | 退出条件 |
| --- | --- | --- |
| A：正常生产调用链 | Mac/Linux 各以合成 ResourcePackage 经实际 Application→TM owner 完成 create 与 replace；覆盖托管目录、一个已登记的 source/default 路径；实际 append/query/export、receipt/registry、冷重开 | 无 Windows-only proof 分派；source package 不变；旧/新 authority、digest/count 一致；不是只运行 standalone SQLite |
| B：协作与崩溃 | 正常 query/append 与 replacement/drain；同资源第二进程竞争；PREPARED、DB_REPLACED、MANIFEST_PUBLISHED、GENERATION_PUBLISHED 的原恢复矩阵各选必要 fault；含连接结束与额外 FD 检查 | 不混用旧 DB 与新家族，不丢正常写回，不出现锁被提前释放或成功先于 final/registry 闭合；沿既有测试增加缺口，不重造矩阵 |
| C：已可观察的 namespace 漂移 | 保留原 `_build_stage` 前真实 rename＋同字节 clone 并继续真实代码；再覆盖首次 mutation 前和应用 publication 前两个边界 | 拒绝旧 preview/lease；替换目录不被应用发布新 canonical/registry/成功 receipt；同字节不是通过条件；不是 mock 直接抛错 |
| D：活事务外部替换的边界 | 延续独立进程替换方式，在 journal 创建/INSERT 后制造变化；实际运行后核对两家族与停止/重开行为 | 结果如实记录；检测到漂移不继续签发未闭合成功；不宣称原字节全保留；原强反例保持失败/不支持性质，不改成产品 PASS |
| E：真实交付回归 | Windows 现有普通 frozen 与 POSIX source 之间的资源包交换、导入后使用及冷重开；受影响共享 Core 和 platform 回归 | 不以 Mac 通过替 Windows 新候选验收；无 Mac/Linux frozen 前置；检索相关变化按原 Gate/兼容性规则验证 |

不需要先上传用户资产、建立完整硬件测评矩阵、重新运行全部历史验收，或为验证本文新建持续审计台账。下一次汇报应只说明：正常跨平台资源能力新增贯通了哪一条、剩余失败落在哪个已批准场景、下一项有限实验消除什么不确定性。

## 9. 最终裁决建议

**可以讨论并收窄的是“非协作外部在线目录替换期间仍全面无损”的承诺；不能收窄的是 LocalCAT 在正常目录中的真实导入、自己协调的并发/替换/崩溃恢复，以及已发现漂移后仍错误接管并报告成功。** 首选修复以上具体接缝并明确残余窗口。若 owner 不接受该窗口，VFS 作为额外能力进入有限试验，而不是用已有失败测试自动立项。

当前结论是架构方向建议，不是已完成的较小修复证明。原分派拒绝必须修好，原候选不能直接恢复，SQLite 内部竞争不能靠前后 stat 冒充解决；以上三点可以同时成立。

## 依据

下列源码链接固定到本轮基线；历史失败链接保留原观察身份。外部文档用于解释接口和风险，不替代目标环境运行证据。

[H]: HANDOFF-POSIX-SQLITE-BINDING.md
[RR]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/resource_repository.py
[QT]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/qt_editor.py
[RP]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/tm_resource_port.py
[M]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/tm_migration.py
[P]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/platform_fs_posix.py
[S]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/tm_sqlite_store.py
[AR]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/tm_activation_recovery.py
[SEAL]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/tm_stage_sealer.py
[CORE]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/.kiro/specs/tm-storage-retrieval-index/design.md
[RPR]: https://github.com/BeFringe/LocalCAT/blob/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5/.kiro/specs/language-resource-portability/requirements.md
[RC]: review/posix-sqlite-binding/rejected-candidate-observations.json
[RCP]: review/posix-sqlite-binding/rejected-candidate/README.md
[O]: review/posix-sqlite-binding/observations.json
[REPRO]: review/posix-sqlite-binding/reproduce_sqlite_namespace.py
[D]: review/posix-sqlite-binding/codebase-discovery.md
[X]: review/posix-sqlite-binding/external-research.md
[C]: https://www.sqlite.org/howtocorrupt.html
[L]: https://sqlite.org/lockingv3.html
[E]: https://sqlite.org/rescode.html
[U]: https://sqlite.org/uri.html
[V]: https://sqlite.org/vfs.html
[VC]: https://sqlite.org/c3ref/vfs.html
[FC]: https://sqlite.org/c3ref/c_fcntl_begin_atomic_write.html
[PR]: https://sqlite.org/pragma.html
[B]: https://sqlite.org/backup.html
[CL]: https://sqlite.org/c3ref/close.html
[EXT]: https://sqlite.org/loadext.html
[PY]: https://docs.python.org/3.14/library/sqlite3.html
[AP]: https://rogerbinns.github.io/apsw/pysqlite.html
[AV]: https://rogerbinns.github.io/apsw/vfs.html
