# POSIX Core snapshot owner 设计发现

## 范围与证据状态

本报告只做代码与合同发现，不选择最终架构，不修改生产代码、正式 R/D/T、ADR 或批准状态。仓库根目录为 `<REPOSITORY>`，调查基线为 `2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5`。先前未通过评审的 `tm_migration.py` 与锁断言候选已由父代理恢复；owner RED 测试和完整候选 patch 继续保存。

用户当前交付方向是补齐 Core 设计并完成跨平台交付：Windows 继续使用 0.5.2 frozen，Mac/Linux 可以 source。RPY 自身 Mac 导出通过不等于 ResourcePackage TM apply 通过；本报告不授予任何运行时能力或成功 receipt。

复用既有 原始 15 项连接实验记录（本文提取其相关观察） 的 15 个实验，没有重跑：Mac 目录 `/dev/fd/<dirfd>/file` 不可用；文件 FD alias 无法完成 DELETE 写事务；命名路径及 `fchdir` 在 INSERT 后目录置换仍可 commit，却清理替换目录 journal、遗留原 journal。这个反例要求修复 SQLite 主 DB 与 journal 的共同对象绑定，不能只替换连接前后 stat。

## 合同与 owner 分工

| 合同 | 本次相关约束 |
| --- | --- |
| `.kiro/specs/tm-storage-retrieval-index/requirements.md:46`，2.4、2.9–2.12 | 失败保持原始资产/先前可用版本；只发布完整 generation；首次失败仍有明确恢复边界 |
| 同文件 `:125`，7.12–7.14 | 显式 import/rebuild 只有完整验证和激活才清除 divergence；失败保持 prior；资源/来源绑定不符拒绝 |
| `.kiro/specs/tm-storage-retrieval-index/design.md:50`，Platform composition / Explicit replacement | Core 独占 SQLite、generation/reservation、journals、LKG、receipt；底层操作消费共享 platform ports，不在 Core 复制 dirfd/OS 原语 |
| 同文件 `:698`，SQLiteTMStore | 每 operation 一次 generation lease、所属线程短连接；DELETE/FULL/FK/5000ms；唯一 stage→seal→registry→activation；四个 phase；成套 recovery |
| 同文件 `:885`，TMMigrationService；`:1212` 附近的 seal epoch / caller-held borrow | 同一 SQL closure、post-fsync 内容与语义证明；关闭连接后同步；borrow 不关闭最外层 W1/root；Windows proof 不能被移植为 POSIX 假保证 |
| `.kiro/specs/language-resource-portability/requirements.md:126`，8.2/8.4/8.6/8.7；`:141`，9.4/9.6 | Application 只调用 TM owner 完整快照面；失败恢复或 recovery-required；成功要求 owner 冷重开与 digest/count/authority 对账 |
| `.kiro/specs/language-resource-portability/design.md:325`、`:487` | Snapshot Ports、create/replace 和 Publication Ordering；registry/runtime/receipt 仍归 Application，不能下移到 VFS |
| `.kiro/steering/adr/adr-020.md` 决策 1/2/3/8/9/12 与长期约束；ADR-025 取代关系 | 平台仅拥有 rooted/lock/publish 原语；业务不得拿 raw dirfd；opaque live identity 不是历史授权；不得把 lease 说成防止非合作写入的 CAS；保留普通 flush/readback/recovery 语义 |

最小 owner 方向仍是 `ResourcePortability → TM owner → shared platform contracts → POSIX native I/O`。新增 SQLite I/O 能力可以服务 Core，但不得成为新的 TM journal、业务 generation 或 receipt owner。

## 可直接复用的能力与限制

以下路径均相对本报告开头的仓库根目录；行号来自上述基线。

| 现有位置 | 可复用部分 | 必须保持可见的限制 |
| --- | --- | --- |
| `platform_fs.py:159` `compose_platform_file_backend` | 按 OS 组合真实 backend、live root self-probe、能力不可用时拒绝 | 新 SQLite 能力尚不在组合 gate 中；不能仅因一般文件 backend 存在就视为 SQLite capable |
| `platform_fs_contracts.py:2037–2119` | `RootedDirectoryAuthority`、`bind_root/open_regular/bind_parent`、`reserve_mutable_file` | 返回 opaque authority，不能让 Core 读取 `_descriptor` / `_directory_fd` |
| `platform_fs_posix.py:2240` `_bind_root`；`:283` `_reprove_directory_chain` | retained root FD；后代逐项相对打开/复证 | 当前 root 直接由给定路径打开，链首 `names[0] is None`；根目录被改名后 reprove 仍可通过。没有把原 configured root 名称完整绑定回 filesystem anchor |
| `platform_fs_contracts.py:1143`；`platform_fs_posix.py:1129` | `MutableFileReservation` 保持 CREATE_NEW 身份，FD 与原 parent/basename 相符 | 没有 SQLite 连接接口、不能直接作为 publisher 的 CandidateFile，也未定义 journal FD 生命周期 |
| `platform_fs_contracts.py:1712`、`:1937`、`:1754` | W1 `LockLease.reprove_binding`；`begin_publish`；`PendingPublication` retained destination / terminal proof | POSIX 是协议锁，不能阻止非合作者另建目录和 W1；publisher 不拥有业务状态机 |
| `platform_fs_contracts.py:2564`；`platform_fs_posix.py:2317` | `ExistingFileDurability` / `BoundSynchronizedRegularFile` 已有 exact bytes、同步和身份复证 | SQLite 活连接期间额外 FD 的 close 必须纳入锁协议，不能只认为 hash 完立即 close 总是安全 |
| `platform_fs_contracts.py:2127`；`platform_fs_posix.py:2638` | `OwnedNamespaceRetirement` 与 `RetainedRetirement` 可处理 attempt 的 exact owned file / quarantine | 不是普通 canonical replace API，也不等于 Windows `ExistingFileRetirement` 的 fresh recovery 能力 |
| `tm_migration.py:10121` `_InitialActivationResourceReservation` | 唯一 root/W1 owner、bound family inputs、release 顺序、初始 attempt 归属 | `_backend is not None` 目前仍混同 Windows portable route；正确分派应基于实际能力/owner合同，而不是删掉 backend |
| `tm_migration.py:298`、`:11530` | `_InitialStageAttempt` 保存 DB/manifest reservations；`_freeze_initial_stage_attempt` rooted CREATE_NEW/partial cleanup | 可以扩展生命周期承载，但 `_build_stage` 后续 Store 连接尚未消费这个 authority |
| `tm_migration.py:316` `_PortableRootedRead` | retained source 流式读取、内容事实 | 名称中的 portable 不使整个 Windows activation codec 中立；同目录 source 必须复用既有 root，避免再次按路径绑定 |
| `tm_stage_sealer.py:131`、`:334`、`:360` | `_CallerHeldSealBorrow`、live stage authority、registry transfer/borrow 的唯一关闭模式 | 完整 `_seal_portable` 产出 portable attestation；不能把它直接塞入 POSIX v2 journal。`:694` 还会为 source 重新 `bind_root(source_path.parent)` |
| `tm_sqlite_store.py:3925`、`:4528`、`:4564` | 唯一 StageSealer 工厂；coordinator 的 activate / activate_replacement、drain/generation/token 状态机 | 能复用业务状态，但物理连接、文件 publication 与 attestation capture 必须接同一个 owner authority |
| `tm_activation_recovery.py:442` / `tm_sqlite_store.py:1759` | `_StoreValidationPort` / `_CoordinatorStorePort` 已把恢复与 Store 的 SQL/验证隔离 | `open_configured_connection(database_path)` 当前只有 Path；write_journal/write_terminal 也仍接 path-bearing legacy writer |

## 必须补的接口语义，不是最终接口命名

### 1. retained SQLite connection scope

现有最窄中央入口是 `tm_sqlite_store.py:18045` `_open_configured_connection`。新增能力需要消费 owner 签发的 live parent、目标 reservation/现有 DB authority、允许的访问模式和生命周期；让 SQLite 实际 main DB、`-journal` 及必要临时文件通过同一个受约束的 I/O owner 打开、锁定、同步、检查和删除。

这里需要两类目标：未发布的 CREATE_NEW stage reservation，以及已激活 canonical/恢复中的既有 DB。只支持 CREATE_NEW 不能完成 activation receipt mutation、reopen、cold recovery 和 replacement prior 验证。

Core 持有应是不可序列化、不可凭字符串重建的连接 borrow。native 层可以内部借用/复制 FD，但共享平台对业务继续保持 opaque；不要把 fd 数字、进程内 token 或 VFS URI 放入 manifest、journal、receipt 或 public DTO。authority 不得通过任意 SQL 参数、环境变量或用户可构造 locator 授予。

`SQLiteTMStore` / `ResourceStoreCoordinator` / 内部 generation view 目前主要携带 stage Path；必须明确如何显式传递连接 scope，防止 schema 初始化有 authority，而随后 batch/receipt/health 重新进入默认路径连接。不能依赖进程全局“当前 owner”或跨线程复用连接。

### 2. 保持原 stage 对象的 rooted publication

`BoundDirectoryAuthority.begin_publish()` 只接 `CandidateFile`。POSIX `_rename_candidate`（`platform_fs_posix.py:704`）明确要求 `_PosixCandidateFile`；当前 `PosixPlatformAdapter:2139` 没有实现 `ExistingCandidateRecovery` 或由 `MutableFileReservation` 转交为发布候选的接口。

与此同时，legacy activation（`tm_activation_recovery.py:3821`、`:4099`）要求 stage / manifest rename 到 canonical 后 inode 和 SHA 都等于 sealed proof。Windows live authority 的 `copy_asset_to_new_candidate()` 不是可直接代换物：复制会改变 inode，违反现有 POSIX v2 的检查。

设计必须决定一个受控的、单次消费或有明确借用期的“已持有 stage 对象发布”能力：保留相同对象，root-relative naming，W1 覆盖目标家族，关闭所有 SQLite 连接且完成 journal 收尾，准确声明命名前/后失败归属，再交给原 coordinator phase machine。可以评估扩展现有 publisher/现有 candidate adoption，而不是另造业务 activation pipeline；不能在 Core 直接调用裸 dirfd rename 规避平台 owner。

### 3. rooted legacy journal / backup / lineage I/O

`tm_activation_journal.py:3360`、`:3507` 已把 journal/terminal writer 和 `:3635` codec 分开；`tm_activation_recovery.py:7436` / `:8815` 仍负责业务顺序。可把底层文件操作接入显式 owner I/O scope，保持 codec 与 phase 决策。

不能漏掉 prior DB/manifest backup（journal `:2565`）、rollback restore（recovery `:4664`）、取消/隔离（journal `:2849`、`:3112`）、terminal/coexisting cleanup、lineage marker（journal `:1016`、`:1526`）和 fresh discovery（recovery `:6539`、`:6599`、`:8364`）。这些是同一成功/恢复闭环，并非只换 `_replace_activation_file:2731` 一处。

### 4. root 身份与 configured locator 的闭合

执行期间 root-relative I/O 必须始终落在原对象，不能命名漂移后操作替换目录。成功发布前还须证明 registry 配置的资源 locator 与本次原 root 一致，否则应进入明确恢复结果，而不是在移走的目录写完就返回成功。

这个“成功时 locator 仍对应原 root”的验证与“实际 I/O 绑定原对象”是两个义务。前后 pathname 检查只能辅助前者，不能代替后者。平台现有 root/后代链需要评估补 origin binding 或 owner 可消费的完整 anchor 链；不要把已经保持 FD 的根目录当前仍存在误当成原名称没有被置换。

## stdlib native VFS 与 APSW 窄 adapter 的实际侵入面

SQLite 官方把 VFS 定义为底层 OS I/O 层，支持同一引擎注册多个 VFS，并按单次 URI 的 `vfs=` 选择；这允许保留上层 SQLite SQL/连接对象，同时替换实际 I/O。它不是把普通路径重写成另一个普通路径即可满足的 wrapper。[SQLite VFS](https://www.sqlite.org/vfs.html)

| 比较项 | stdlib `sqlite3` + 可信内部 native VFS | APSW + owner 窄 adapter |
| --- | --- | --- |
| 返回对象 | 可以继续由现有 `sqlite3.connect` 生成 exact `sqlite3.Connection`；本报告尚未做本仓库 native 可行性 spike | 返回 APSW 对象，或自定义 wrapper；都不能满足现有 exact `sqlite3.Connection` 检查 |
| SQL/tuple/cursor | 上层 SQL、默认 tuple 行、cursor 通常无需改；仍要把所有 owner reopen 引向指定 VFS | 可维持 SQL文本，但须实现并测试既有 cursor/事务/异常/backup 语义兼容 |
| SQLite 实例 | 可信 extension 入口提供的 `sqlite3_api_routines` 可把注册调用定位到加载它的那一个 SQLite 引擎 | 不能假设 APSW wheel 与 CPython `_sqlite3` 共用一份 SQLite；即使版本号相同也不是证明 |
| 修改位置 | Core连接分发/borrow + 新平台 native owner + 构建/加载/资格；SQL validators 的 exact-type 约束可保留 | 除上述 authority 工作外，还触及 Store、Sealer、candidate projection、recovery error mapping、schema backup 等驱动 API 边界 |
| 现有 runtime 准入 | 当前明确关闭 extension loading；需要受控内部扩展例外、可信产物加载与失败关闭合同 | 新驱动及其 SQLite build/FTS5/异常/锁语义是新增依赖与资格输入，不能把它当一般纯 Python helper |
| Windows 0.5.2 | 新 POSIX扩展应通过精确 composition 隔离，已冻结 EXE 不被热补丁/替换依赖 | 不能为了 Mac/Linux adapter 改写 frozen Windows 的驱动或假称旧 EXE含新代码；共享源仍需回归隔离 |

APSW 官方明确其与 stdlib 并非同一个 DB-API 层，事务、返回对象及错误处理存在差异；其文档可用于形成明确 adapter 验收面，不能作为“直接替换 connect”依据。[APSW 与 sqlite3 差异](https://rogerbinns.github.io/apsw/pysqlite.html)

### 同一 SQLite 实例与扩展生命周期

SQLite loadable extension 的固定入口接收 `sqlite3 *db` 和 `sqlite3_api_routines *pApi`；用 `sqlite3ext.h` / `SQLITE_EXTENSION_INIT2` 注册 VFS 可避免自行链接第二个 SQLite 引擎。这是父任务候选的技术依据，不是本次已实现的能力。[SQLite extension ABI](https://www.sqlite.org/loadext.html)

必须在设计中解决：扩展仅由可信内部路径和确定 entrypoint 加载；固定产物身份；不接受 ResourcePackage/用户路径；初始化失败禁止 fallback；扩展代码生命周期不得早于任何 VFS connection/borrow；初始化 API如何把 opaque平台 authority 转交 native registry，以及如何撤销。SQLite 文档明确普通 extension 默认随加载连接关闭而卸载，VFS可用永久加载机制；不能初始化一个内存连接、立即关闭，再留下指向已卸载代码的 VFS。[SQLite persistent extensions](https://www.sqlite.org/loadext.html#persistent_loadable_extensions)

仓库 `detect_sqlite_runtime()`（store `:17994`）和各 connect helper 调用 `enable_load_extension(False)`。Python 文档也指出某些 Mac Python 构建根本不启用 loadable extension 支持。当前实验的 Python 能运行 Core 不等于所有 source 安装都满足新 native loader gate；需要独立构建/运行时能力与错误合同。[Python sqlite3 扩展支持](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.Connection.enable_load_extension)

### 多份 SQLite 与同 inode FD close

SQLite 官方列明同进程多份 SQLite 引擎会各自维护 open-file/锁状态，某份库 close 可破坏另一份库的 POSIX advisory locks。因而不能只让 stage 使用 APSW而同一 canonical 的 health/recovery/backup继续由另一份 stdlib SQLite 打开；需要统一引擎/驱动，或单独证明严格隔离。另一引擎注册的 VFS也不会自动成为 stdlib那份引擎的 VFS。[SQLite 同进程多副本风险](https://www.sqlite.org/howtocorrupt.html#multiple_copies_of_sqlite_linked_into_the_same_application)

**同一个引擎仍不是全部证明。** 自定义 VFS 的锁实现、现有 unix VFS、外部 reservation/hash/synchronization FD都必须遵循一致的同 inode关闭规则。普通 POSIX close 可能取消该进程对同 inode的记录锁；新能力必须防止 attestation/borrow关闭某个FD时，别的同 inode SQLite连接还在事务中。不能以 W1 的 flock 取代 SQLite 的页锁/rollback-journal并发合同，也不能改用 no-lock VFS。[SQLite POSIX close 锁风险](https://www.sqlite.org/howtocorrupt.html#posix_advisory_locks_canceled_by_a_separate_thread_doing_close_)

## 当前 Python SQLite API依赖的精确清单

### exact-type 门

| 文件 | 位置与用途 |
| --- | --- |
| `tm_sqlite_store.py:18027` | `_configure_immediate_seal_bulk_cache`：`type(connection) is sqlite3.Connection` |
| `tm_stage_sealer.py:2801` | `_mark_stage_sealed`：同样的 exact-type 检查 |
| `tm_sqlite_candidate_projection.py:1734` | `candidate_proof_projection_digest`：同样检查 |
| `tm_sqlite_candidate_projection.py:1803` | `_validate_candidate_proof_index_core`：同样检查 |

APSW wrapper不是 sqlite3.Connection 的子类，更不是 exact type；改这些检查应是明确的 Core连接契约变更，不应放宽为 `hasattr(execute)`。

### 事务、行与错误

- 对本次相关 Core模块及生产 Python文件的搜索没有发现 `sqlite3.Row`、`row_factory` 或 `text_factory` 配置；实际默认行是 tuple，频繁使用序列解包、位置索引和精确 tuple比较。因此 Row factory 不是当前首要迁移阻碍，不能凭习惯声称已有依赖。
- 连接依赖 `execute/executemany`、cursor iteration、`fetchone/fetchall/fetchmany`、`rowcount`、`lastrowid`、`in_transaction`、`commit/rollback/close`、`enable_load_extension(False)`。例如 activation receipt在 recovery `:4045/:4074` 以 rowcount==1 验证一次转换；store `:16882` 消费 lastrowid；`:10532/:10548` 验证 transaction scope。
- `isolation_level=None` 配合显式 `BEGIN` / `BEGIN IMMEDIATE`，异常路径手工 rollback；当前 Python 3.14 默认事务策略不应在换驱动时被隐式改变。API adapter必须保留无事务 commit/rollback、异常中 rollback、cursor消费后结束时机等行为。
- `sqlite3.Error/DatabaseError/OperationalError` 被多层捕获；store `:18197` / `:18230` 读取 `sqlite_errorcode` 并 `& 0xFF` 映射 BUSY/LOCKED，另有字符串兼容分支。APSW adapter必须明确扩展错误码、retryable 与因果链映射，不能把所有错误压成通用 RuntimeError。
- schema upgrade依赖真正 `Connection.backup()`（`tm_schema_upgrade.py:211/:220/:1039/:1045`；migration `:7785/:7794`）。不同驱动对象不能直接当成彼此的 backup target。
- FTS5/trigram capability从当前引擎 `:18007` 的 `:memory:` probe获得；切换数据引擎后，不能继续用 stdlib probe为 APSW授予FTS能力。
- 本次相关模块未发现 user-defined SQL函数/排序器、progress/authorizer/trace callback构成现有连接必需面；VFS本身也不应新增能从业务SQL获取原生authority的函数。

### connect 入口覆盖面

静态 AST盘点下面四个文件共 21处 `sqlite3.connect`，其中 1处内存FTS probe、20处文件连接。单一 helper改动不能覆盖全体。

| 文件 | 直接 connect行号 | 集成含义 |
| --- | --- | --- |
| `tm_sqlite_store.py` | 3103、8246、18007（内存）、18058、18259、19383、19426、19470 | completed authority读、portable回读、通用连接、health、upgrade前置读；另有 `_open_leased_connection:18197` 间接入口 |
| `tm_stage_sealer.py` | 1487、1506、2838 | readonly sealed proof、SEALED事务、失败恢复 UNPUBLISHED；三者均不能逃出owner scope |
| `tm_migration.py` | 7785、7794、9804、11109 | upgrade backup和sidecar/reusable stage检查；阶段复用也须绑定同一实际对象 |
| `tm_schema_upgrade.py` | 211、220、814、866、1039、1045 | backup、marker/数据迁移、copy；是否与本轮完整连接能力一起迁入，需要显式边界裁决 |

`tm_activation_recovery.py` 没有直接 connect，它经 `_StoreValidationPort.open_configured_connection` 进入，适合维持既有依赖方向；`tm_sqlite_candidate_projection.py` 接受连接并执行SQL，不应成为连接或native authority owner。

## POSIX 原 journal 格式能否保留

**具备保留格式的结构条件，但必须保持对象与状态语义；尚不能写成已证明兼容。**

- `tm_activation_journal.py:443/:464` 的四 phase 与 `activation-journal-v2`，`:586` frozen record、`:3635` payload、`:3734` canonical serializer、`:3863` parser、`:4169` validator可继续拥有原语义。文件路径在record中是locator及digest输入，record注释已明确其自身不携带路径authority。
- 新 live borrow/FD/VFS token只影响运行时I/O，不必因此加入v2字段。重新加载仍先取得fresh parent/W1、重绑精确资产并验证现有bytes/phase/owner闭集，不能从历史inode或同字节copy直接恢复live authority。
- `SealedContentAttestation` / `ActiveContentAttestation` 仍是legacy writer要求的exact类型；`_require_legacy_content_attestations_for_durable_write:3332` 和 `:4169` 不接受portable类型。因此应评估“从既有retained对象产生原legacy proof”的内部capture接口，而非把Windows portable proof转换或伪造为POSIX proof。
- `tm_content_attestation.py:1350` 已有完整no-follow链capture，但它从Path重新开始；`:1611` 的platform capture也重新bind root。都需要可消费caller-held root/已保留文件的接口。`FileObjectIdentity`是opaque；Core不得靠解析其字节编码拼装device/inode。若保留v2表示，平台/native到Core原ContentFileProof的可信投影必须明确归属。
- canonical发布必须保持原sealed DB/manifest对象，才能继续使用现有 `candidate_stage_db_identity` 和 sealed inode+SHA检查。若方案改成copy新对象、改路径规范化、换phase、换proof类型或把旧record重新解释为新authority，就不再是纯I/O替换，需具体codec/迁移设计。
- Windows v3 / replacement codec明确要求 `WindowsPrivateProof`（journal `:4622/:4971/:5485`）；不应为实现POSIX而放宽这些类型或fresh recovery门。Windows 0.5.2的既有格式独立保留。

## 必须串联的生命周期

1. Application持有preview及资源计划；TM owner取得一次root/W1。所有后续Coreborrow都绑定该resource、parent与锁实例，不重新拿另一份配置路径上的锁作为等价物。
2. 通过同一root捕获prior并发布source candidate；source `PendingPublication`与prior恢复事实保持到owner callback/terminal。package私有payload另有只读root，不与本地authority混淆。
3. 创建DB/manifest reservations，attempt唯一拥有base authority。每次schema/batch/receipt SQL连接借用scope；native句柄不得比borrow早关闭，也不得反向关闭caller reservation。
4. 完成SQL事务、所有cursor/connection关闭、DELETE journal确切终结；随后才执行seal所需同步、最终只读验证和exact hash。VFS作用域撤销需要确认没有未关闭连接/statement或pending I/O。
5. StageSealer沿同一root/reservations取得source、DB、manifest事实，完成既有语义/内容epoch；registry接管必要live证明。两次Gate B和token单次消费继续保持，borrow只撤销自身，不释放base W1/root。
6. coordinator drain旧generation；捕获/备份prior，写PREPARED；通过保持对象的rooted发布切换DB，再在同一authority下连接更新receipt，发布manifest、generation与lineage/terminal。
7. 成功callback中的`project_applied_owner()`（migration `:3923`附近）及`TMResourceSnapshotPort`的`commit_before_terminal`（port `:307/:382`）必须消费当前owner scope。cold reopen可以是独立连接/验证，但不能悄悄按替换路径重新铸造root/W1而把另一目录当本次成功。
8. registry/runtime/operation receipt提交结果与Core durable结果对账。若新canonical已证明发布，晚期失败只能按既有journal/receipt决策恢复，不能adapter猜测回滚source。所有cleanup/terminal/retirement关闭仍在同一W1内，最后才释放base root/lease。
9. fresh process recovery从零取得能力与W1，不能复用序列化FD或旧native token。loader/VFS初始化、journal-family打开与关闭、legacy phase重放和terminal proof均要真实覆盖。

## 对侵入面与验证计划的发现

必要修改面不应只按文件数量判断。至少涉及共享platform合同/适配、Core连接入口与内部scope、migration编排、sealer/attestation、activation journal/recovery；相邻schema upgrade/direct reopen入口需明确纳入或证明不会与新连接通道同时操作同一文件。`tm_resource_port.py`应尽量保留公开DTO和错误投影；ResourcePackage carrier/schema不需要因VFS而改变。

新native代码/扩展的build recipe、产物身份、SQLite ABI/compile options和运行时初始化会成为资格输入。`tm_benchmark.py:106` 的实施来源闭集目前已覆盖platform与Core Python模块，`:814` 生成implementation fingerprint；新native输入不能漏出闭集或手工改历史证据。Mac/Linux source需重新走对应owner资格；Windows 0.5.2 frozen只代表其原已冻结执行资产，不能继承新POSIX代码资格，也不能被新source fingerprint追认成新EXE。

后续设计需给出下列可验证选择，而不是本报告代选：

- exact stdlib连接能否通过受控内部扩展注册同一引擎VFS，以及明确extension信任/加载例外；或是否接受驱动协议迁移及统一SQLite实例的成本。
- main/journal/temp I/O、锁与FD close规则由哪个平台native owner保证；所有同文件连接如何保持锁协议一致。
- 保持stage inode的rooted publication接口采用何种现有能力扩展；legacy proof的可信转换边界在哪里。
- root被移走时如何判定配置locator一致性与恢复结果；不能向替换目录发布canonical，也不能把旧root完成自动当registry可见成功。
- 持久v2字节/phase保持的准确范围，以及现有POSIXcompleted/recovery资产的兼容矩阵。

验证必须包括真实CREATE_NEW/REPLACE、callback/receipt/cold reopen、先前owner RED、每个SQL连接与journal时序中的父目录置换、两进程W1/SQLite锁、commit/rollback/close故障、进程kill与fresh recovery。跨平台交付还需Mac/Linux source与Windows 0.5.2 frozen的真实ResourcePackage双向旅程；不因RPY导出happy path或单一native spike通过省略Core资格。

## 发现状态

代码发现已完成；未实施、未验证新VFS/驱动能力、未选择最终方案。所有本轮持久写入仅为本文件。既有15个实验作为负证据复用，生产与正式RDT未改。
