# POSIX SQLite 连接绑定：官方资料研究

研究日期：2026-10-12。本文为设计研究输入，不是正式 RDT、实现完成声明或审批记录。范围是 Mac/Linux source 运行时；Windows 沿用现有 frozen 路线。未安装依赖、未加载 SQLite 扩展、未修改生产文件；后续仅编译运行了合成文件上的独立 OFD 能力探针。`[资料]` 表示官方文档或源码事实，`[观察]` 表示本地清点/有限探针，`[推导]` 表示待设计和实验验证的结论。

## 结论供主设计者选择

1. `[推导]` SQLite 的 VFS 接口可以覆盖实际 DB、rollback journal、临时文件及删除操作，能够表达“持有同一目录 authority，在其下完成全部 I/O”的机制。路径字符串、`fchdir` 或只在连接前后核对 inode 不提供这个机制。
2. `[推导]` **可信内部 C VFS，通过宿主 `sqlite3_api_routines` 注册**，是保留现有 stdlib `sqlite3.Connection`、`Row`、异常和事务语义的较小集成候选。它新增 native 构建和内部扩展加载例外；VFS 的锁、同步、异常及关闭实现并不“小”。当前研究不等于已证明可交付。
3. `[推导]` **APSW + Python 自定义 VFS**免去自写 C/Python 回调胶水，但仍需自写文件/锁语义；APSW 的官方二进制仍是 native 依赖。PyPI 构建有独立 SQLite 实例，不能只替换 import 写连接而继续让 stdlib 同进程访问同一 DB。整体 Core 连接适配和回归面更大。
4. `[推导]` 内存 stage + serialize 可减少 stage 构造时的路径 I/O，但没有修复 canonical 读写/热 journal/恢复路径，且整库复制与 100k/RSS 合同存在需验证的成本，不宜作为完整修复的默认方案。

## 1. VFS 是能力落点，但不能只包装 xOpen

`[资料]` SQLite 将文件系统操作交给 VFS，可按连接显式选择 VFS；VFS 不必成为进程默认。VFS 对象处理 `xOpen/xDelete/xAccess/xFullPathname`，文件对象处理 read/write/truncate/sync/lock/close。[SQLite VFS 总览](https://www.sqlite.org/vfs.html)

`[资料]` `xOpen` 的类型标志区分主 DB、主 journal、临时 DB/journal、subjournal、super-journal 和 WAL；空文件名表示 VFS 自行分配并在关闭时删除的临时文件。文件名由 `xFullPathname` 结果加 SQLite 后缀派生。`xSetSystemCall` 是可选测试接口，其可替换调用集合会随版本变化，不宜作为稳定产品合同。[sqlite3_vfs 合同](https://www.sqlite.org/c3ref/vfs.html)

`[推导]` 建议的有限机制是：连接拿到不可序列化的 binding token，VFS 将 token 映射到仍存活的 directory fd、DB reservation/identity 和允许的文件角色；文件操作使用 `openat/fstatat/unlinkat` 一类目录相对操作，以及已打开文件 fd。`xFullPathname` 产出内部稳定名称，不再拿展示路径重新取得 authority。未知 token、越界后缀/文件类型和不受支持模式失败关闭。token 注册、撤销、引用计数与跨线程访问必须有明确协议。

`[推导]` `xDelete` 和 hot-journal 检测必须使用相同 binding；不能让 DB 用 fd、journal 用普通 pathname。dirfd 只能防止跟随“原路径上的替换目录”；它不自动防止保留目录内部的同名文件替换。文件 identity、nofollow、创建预留、仅清理本次拥有对象，以及被撤销后允许的 rollback/close 行为仍需设计。POSIX `stat` 后 `unlinkat` 也不是针对非协作攻击者的原子 compare-and-delete；不能因此虚构全能 OS CAS。

`[资料]` rollback journal 保存事务前内容，通常与 DB 同目录、名为 `DB-journal`；打开 DB 可能先执行 hot-journal rollback。故“读连接”也可能触发恢复写入，不能按 SQL 是 SELECT 就认定底层无写。[SQLite rollback 锁与恢复](https://www.sqlite.org/lockingv3.html)

## 2. 可信 C 扩展能否保留 stdlib API

`[资料]` SQLite 官方扩展入口接受宿主 `sqlite3_api_routines *pApi`；使用 `sqlite3ext.h` 和 `SQLITE_EXTENSION_INIT2(pApi)` 后可调用宿主的 `sqlite3_vfs_register`。官方明确支持通过扩展注册 VFS；返回 `SQLITE_OK_LOAD_PERMANENTLY` 可使其代码在初始连接关闭后继续驻留。[官方扩展入口与持久加载](https://www.sqlite.org/loadext.html)

`[推导]` 候选组合流程：受控 bootstrap `sqlite3` 内存连接加载项目自带的内部库；库只通过 `pApi` 访问该宿主 SQLite，不再链接/`dlopen` 另一份 SQLite；注册唯一、非默认 VFS。后续仍用 stdlib `sqlite3.connect(..., uri=True)` 显式指定该 VFS，因此 Python 层类型与原有事务 API 不必改成 APSW。这里保留的是接口，新增 SQLite I/O 错误及撤销状态仍需要 Core 映射。不能把 `ctypes.CDLL('libsqlite3...')` 注册的 VFS 视为会出现在当前 `_sqlite3` 实例中。

`[资料]` Python 的 loadable SQLite 扩展能力取决于构建，并非所有 macOS Python 都有。当前 Python 提供 `load_extension`、`setconfig`；运行时能力仍应检测并明确失败，不能静默回退路径连接。[Python 3.14 sqlite3 API](https://docs.python.org/3.14/library/sqlite3.html)

`[资料]` `SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION` 可只启用 C 加载 API、保持 SQL `load_extension()` 关闭；传统 `sqlite3_enable_load_extension()` 会同时影响两者。[SQLite db_config 合同](https://sqlite.org/c3ref/c_dbconfig_defensive.html)

`[推导]` 设计可限定 bootstrap 为代码拥有的内存连接，使用固定内部库及指定入口，加载后立即关闭 C 加载开关；普通数据连接始终关闭扩展加载。实际 Core 设计现写“SQLite extension loading 保持关闭”（`tm-storage-retrieval-index/design.md:1314`），这仍是**显式设计增量**，不能称作原合同下的实现细节。source 交付允许使用 native 组件，但不等于免除 macOS arm64/x86_64、Linux ABI/架构、编译器和交付文件定位问题。若提供可复现预编译库，可免用户本地编译；此研究未生成任何库。

`[推导]` 必须验证扩展只绑定一个宿主实例；同一扩展映像若被第二个 SQLite 实例初始化，不得覆盖共享 `pApi` 指针继续服务第一实例。内部 registry 不接受从项目包恢复的 fd/token，也不把原始文件 authority 变成任意 SQL 可调用权限。注册成功、选中 VFS、具体连接实际使用该 VFS须有可观测证据。

## 3. APSW + Python VFS 的实际边界

`[资料]` APSW 支持 Python `VFS` 以及自定义文件对象；继承 `VFSFile` 主要用于复用已有 VFS 文件实现。`Connection(..., vfs=...)` 显式选择；VFS 回调异常会转成 SQLite 错误，但部分清理错误可能被 SQLite 忽略。APSW 不提供现成的 dirfd authority 实现。[APSW VFS API](https://rogerbinns.github.io/apsw/vfs.html)

`[推导]` 用 Python `os.pread/pwrite/fstat/ftruncate/fsync` 和目录相对 `os.open/unlink/stat` 实现文件对象在 API 上可行；`fcntl` 锁布局、完整锁状态机、跨连接计数、生命周期仍由项目承担。仅覆写 `xOpen` 后交给默认 `VFSFile` 用名称再开文件，会重新引入未绑定路径。没有公开合同允许给默认 unix VFS 直接注入已持有 fd 并自动复用其内部锁表。

`[资料]` APSW 当前文档覆盖 Python 3.10–3.15。PyPI 版本私有静态内嵌 SQLite，与进程内其他 SQLite 分离；它与 DB-API `sqlite3` 的事务、异常、cursor、context manager 行为不同。同一 DB 在非 Windows 进程内由两份 SQLite 打开被明确警告不安全。[APSW 与 sqlite3 差异](https://rogerbinns.github.io/apsw/pysqlite.html)

`[资料]` 常见平台有预编译包；无匹配包则从源码编译，需要 C 编译器和 Python headers。发行版 APSW 可能动态链接系统 SQLite、版本也可能落后，不等同于 PyPI 构建。[APSW 安装与编译](https://rogerbinns.github.io/apsw/install.html)

`[资料]` APSW 在耗时 SQLite 调用期间释放 GIL；同一连接不提供真正并发执行，跨线程同时使用可能报 `ThreadingViolationError`，同一 cursor 不能并发，连接不能被两线程同时关闭。[APSW 执行/线程模型](https://rogerbinns.github.io/apsw/execution.html)

`[推导]` 采用 APSW 应迁移所有可能接触同一 canonical/stage/recovery inode 的 Core 连接及探测者，封装所需的 DB-API 差异，并重新跑实际 Core correctness/FTS/fallback/性能门。不能写“Python VFS 因为有 GIL 所以不需要锁”。Python 回调会增加页 I/O 往返与对象分配；是否满足 100k 门需实测，不能从文档推断性能通过。

## 4. 最容易漏掉的是锁、fd close 与关闭完成

`[资料]` POSIX 传统 advisory record locks 可能因同进程另一 fd 的 `close()` 被整体释放。SQLite unix 实现以 inode 表、连接计数和延迟 close 协调；两份 SQLite 不共享该表会破坏保护。使用不同锁协议的连接也不能安全协调。[SQLite 官方损坏情形](https://www.sqlite.org/howtocorrupt.html)

`[资料]` 官方 `os_unix.c` 的 `unixInodeInfo` 维护 inode/锁/引用数，`UnixUnusedFd` 保留应延迟关闭的 fd，最后一个锁释放时才回收。[SQLite unix VFS 源码](https://sqlite.org/src/doc/trunk/src/os_unix.c)

`[推导]` “同一 SQLite 实例”是 C 扩展路线的重要条件，但**不自动让自定义 VFS 加入默认 unix VFS 的私有锁表**。若自建传统 POSIX 锁，所有同 inode 的自定义连接，以及 Core 为取样/验证而开的原始 fd，都需要协调关闭；同进程默认 VFS 与自定义 VFS 混用仍应禁止或经明确互操作设计验证。不能简单用 `flock`、空锁函数或外层 W1 lease 替换 SQLite 的 SHARED/RESERVED/PENDING/EXCLUSIVE 语义。

`[资料+观察]` 不应宣称“Mac 无 OFD locks”。当前 Apple XNU 主线头在公开 Darwin 区域定义 OFD 常量；CPython 3.14 根据编译头的 `#ifdef` 暴露常量。本机确实有 `F_OFD_SETLK=90`、`F_OFD_SETLKW=91`、`F_OFD_GETLK=92`。Python 文档仍仅明确说明 Linux ≥3.15 的支持，旧 Apple 头曾放在 PRIVATE；详见后续有限探针。[Apple 当前头](https://raw.githubusercontent.com/apple-oss-distributions/xnu/main/bsd/sys/fcntl.h)、[CPython 条件导出](https://raw.githubusercontent.com/python/cpython/3.14/Modules/fcntlmodule.c)、[Python fcntl API](https://docs.python.org/3.14/library/fcntl.html)

`[推导]` OFD 可以作为需单独证明的策略：最低支持系统、目标文件系统、Mac/Linux `struct flock` ABI、与传统 POSIX 锁互斥性、dup/fork/close语义都要实测。常量存在不足以批准整条路线，不能硬编码命令数值当通用 POSIX 能力。若采用可移植传统 locks，就明确承担 inode registry/延迟 close，而非把它藏进“轻量 bridge”。

`[资料]` `sqlite3_close_v2` 可先使连接不可用，直到未完成 statements/BLOB/backup 释放后才真正销毁。最终销毁时仍会回滚未结束事务。[SQLite close 合同](https://sqlite.org/c3ref/close.html)

`[推导]` 借用释放不能仅以 Python `connection.close()` 返回为依据。VFS 应跟踪真正 `xClose` 和所有 journal/temp 文件句柄，在最后 I/O 完成后再释放 retained directory/file authority；Core 先结束 cursor/BLOB/backup。取消/关闭不能由另一线程抢先关 fd。出错时仍需区分“可安全回滚原对象”“只可关闭保留证据”“不得清理不属于本次的文件”。

## 5. DELETE、FTS5、WAL/ATTACH 和临时文件

`[资料]` `sqlite3_io_methods` 的 version 1 提供 rollback 所需文件操作；version 2/3 分别增加共享内存和 mmap 接口。短读必须补零；sync flags 区分普通 sync、Mac 风格 fullsync 和 data-only；不认识的 file-control 返回 `SQLITE_NOTFOUND`。[SQLite 文件 I/O 合同](https://www.sqlite.org/c3ref/io_methods.html)

`[推导]` 限定 DELETE rollback、禁 WAL/mmap/ATTACH 能收窄首版机制，符合当前 Core 已有 `journal_mode=DELETE/synchronous=FULL` 前提。但需要连接层配置后复核、authorizer/允许 SQL 集合和 VFS 文件类型拒绝共同保证；不能只执行一次 PRAGMA 后假定永远不变。`xShm` 不实现并不替代对已有 WAL DB 的明确拒绝/恢复政策。不得把 `journal_mode=MEMORY/OFF` 当作规避 journal 绑定的修复。

`[资料]` FTS5 内容和索引保存在同数据库的 shadow tables。[SQLite FTS5 数据结构](https://www.sqlite.org/fts5.html#fts5_data_structures)

`[推导]` FTS5 与 DELETE VFS 在机制上兼容，不需要额外文件格式或 ATTACH；仍需对应编译内置 FTS5、trigram tokenizer 与既有 fallback 实际验证，不能因 APSW“带扩展”就跳过 Core capability/gate。

`[资料]` SQLite 除主 rollback journal 还可能使用 statement journal、TEMP DB、排序/瞬态索引、VACUUM 临时库；这些临时用途不是永不变化的承诺。[SQLite 临时文件](https://www.sqlite.org/tempfiles.html)

`[推导]` 禁 ATTACH 去掉 super-journal 的主要来源，但不自动去掉其他临时文件。最小路线必须列清真实 Core SQL 所需类型，对其使用受绑定私有 scratch 或有界内存策略；完全拒绝未知类型失败关闭。单纯设置 `temp_store=MEMORY` 可能增加 RSS，应纳入性能门。官方 demo VFS 有意省掉锁/部分临时文件/截断，不可直接当生产实现。[SQLite demo VFS 的明确省略项](https://sqlite.org/src/doc/trunk/src/test_demovfs.c)

## 6. 内存 serialize/deserialise 的受限备选

`[资料]` SQLite serialize 通常分配整库序列化副本；NOCOPY 只在已有连续内存表示时返回指针，否则返回 NULL。[SQLite serialize](https://www.sqlite.org/c3ref/serialize.html)

`[资料]` deserialize 将数据库转成以提供缓冲区为基础的内存数据库，增长需要可调整缓冲区；不支持直接使用 WAL 格式内存映像。[SQLite deserialize](https://www.sqlite.org/c3ref/deserialize.html)

`[资料]` CPython 3.14 serialize 最终构造 Python `bytes`；deserialize 用 `sqlite3_malloc64` 分配并复制 Python 输入。其 `Connection.close` 使用 `sqlite3_close_v2`。[CPython sqlite3 连接源码](https://raw.githubusercontent.com/python/cpython/3.14/Modules/_sqlite/connection.c)

`[推导]` 有限 fresh-stage 方案可以在内存完成构造，再把 bytes 写入已预留 fd，关闭并验证后发布；但要明确新 durable-stage 边界和失败处理，且 published canonical 常态读写/热恢复仍未解决。峰值可能同时包括内存 DB、SQLite 序列化缓冲区和 Python bytes；“大约几倍库大小”仅是风险估计，不是实测值。Core requirements 8.1–8.4 保留 100,000 记录、查询/迁移时间和 RSS ≤512 MiB；因此此选项不能以无限内存或小样本结果替代通用连接设计。

## 7. 采用与自建比较

| 路线 | 可保留的现有能力 | 必须新增/承担 | 目前判定 |
|---|---|---|---|
| 内部 C VFS，经宿主 pApi 注册 | stdlib Connection/Row/异常、同一 SQLite 引擎与编译能力 | native 源码/构建、受控加载例外、authority registry、完整有限 VFS、锁/关闭/同步 | 集成改动较小的优先验证候选；尚未实现或证明 |
| APSW + Python 文件对象 | SQLite SQL/文件格式；成熟 Python↔VFS 胶水 | APSW native 依赖、Core DB-API 适配、同DB统一引擎、自己实现 fd/锁/同步、Python回调性能 | 可行备选；不是现成安全 VFS，也不是零 native |
| 自有 CPython binding + 自带 SQLite | 可完全控制引擎/VFS | SQLite版本维护、DBAPI/行/异常适配、native构建、并存隔离 | 比内部 loadable VFS 更大，当前没有必要先选 |
| 复制/改造 SQLite unix VFS | 可参考成熟锁/同步算法 | SQLite私有结构与版本耦合、维护派生代码、仍需 rooted命名空间设计 | 可作实现参考，不直接承诺可无缝复用 |
| 内存 stage + serialize | stdlib API；fresh-stage无DB磁盘I/O | 整库内存峰值、持久化边界；canonical/recovery仍另解 | 仅受限场景备选，不能作为完整修复 |

## 8. 在实施前必须变成可执行证明的项目

1. **同实例注册**：真实 source Python 加载自带库、非默认 VFS 被选中；无加载能力/错误架构/ABI/重复宿主初始化时明确拒绝；普通 SQL 无加载权。
2. **真实 SQLite 继续执行**：在 DB open 前、journal open 前、首次 INSERT 后/commit 前、journal delete 前、rollback、`xClose` 时做 parent rename+clone；比较原/替换 DB 与 journal，任何结果都不能把替换目录当作原 authority。
3. **子项替换与撤销**：DB、journal 同名替换、symlink、hardlink、旧 token、fd复用；错误后不可按新名称误删或发布。Core 要保存 sticky invalidation/失败证据，不能只信 SQL commit 返回值——某些 VFS 清理错误会被忽略。
4. **锁与生命周期**：同进程多连接/多线程、第二进程、验证 fd 关闭、连接取消、延迟 cursor finalize；锁降级/释放不影响其他持有者，事务内关连接按政策回滚，authority 不提前释放。
5. **同步与恢复**：SHORT_READ补零、部分写、EINTR、ENOSPC、文件sync/目录sync失败、热journal冷重开，保留未知状态而非伪报成功；FULL sync在具体Mac/Linux路线上验证。
6. **功能/性能**：CREATE_NEW、REPLACE、常态读写、FTS5与fallback、Core现有真实100k门；WAL/ATTACH/未知临时模式应产生预期拒绝，无新静默回退。
7. **交付边界**：Mac/Linux source能构建或取得相应内部组件并运行同一场景；Windows frozen不加载此POSIX库，并复测现有路线。尚未使用当前研究签发任何资格。

## 本地只读清点

这里只摘录与方案相关的运行时事实；完整机器清点不随分享件提供。本机 Python 3.14.7，SQLite 3.53.1，macOS 26.2 arm64；`_sqlite3` 是内建模块，无可供默认推断动态链接路径的 `__file__`。存在 extension/serialize API 和编译内置 FTS5；APSW/CFFI 未安装。`pread/pwrite/fsync`、所需 `dir_fd` 接口可用，`fdatasync` 不存在。此清点仅证明 API/常量可见，不证明 VFS、锁协议或崩溃恢复合格。

## 后续 OFD 有限实验

见 [OFD 发现报告](ofd-probe/report.md)。在当前 macOS 26.2 arm64/APFS，用 SDK 26.5 公开头编译的独立探针完成 21 个检查：独立 open 互斥、OFD 与传统锁同进程/跨进程互斥、关闭无关 fd 不丢 OFD、dup 最后关闭释放、共享/独占升级均符合预期；传统锁对照则确认无关 close 会释放锁。此结果收窄了当前 Mac 路线的未知，不代表完整 SQLite VFS 已成立。尤其不能给多连接 dup 同一 fd 后误认为有独立 OFD 锁；最低旧 Mac 版本、Linux 实测、目标卷能力仍未证明。
