# POSIX TM 连接与目录绑定：外部架构评审交接

代码基线：[LocalCAT @ 2e1cbce](https://github.com/BeFringe/LocalCAT/tree/2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5)。这是一份临时评审输入，不是正式 Requirements / Design / Tasks，也不授权实现或改变既有审批。只使用合成数据；本次不附用户游戏、翻译资产、设备资格、凭据或设备私有密钥。压缩包只包含本文与支撑件，生产代码从上述基线读取。

## 1. 需要裁决的核心问题

LocalCAT 要完成 TM ResourcePackage 的跨平台导入。Windows 已有 0.5.2 普通 frozen 路线；Mac/Linux 可以保持 source，不要求新的 Mac/Linux 自包含发行。

请先判断下面的故障、既有承诺和修复成本，而不是接受“必须自建 SQLite VFS”这个前提：

1. **更小的修复是否存在？** 在保持已有 rooted 命名操作、完整资源快照事务和诚实失败语义下，是否能用现有 SQLite / 平台能力闭合？需要指出主数据库及 journal 的真实 I/O 机制，不能只建议连接前后 stat。
2. **现有承诺是否有不必要的扩大？** 区分普通用户资产保护、已声明的目录替换反例与任意同用户恶意进程的强对抗。哪些必须保留，哪些如需调整应明确成为产品决定？不要把测试数量或已存在的防御代码当成不可质疑需求。
3. **如果必须增加连接能力，哪条路线成本最低？** 比较标准库 `sqlite3` 加内部 VFS、APSW 加自定义 VFS、受限内存 stage，以及其他有具体机制的方案。请评价长期锁/恢复/构建维护成本，不能只比较接口代码行数。
4. **如何有界验证后再定案？** 给出一个最小可行性实验及退出条件：成功能消除什么不确定性，失败就停止哪条路线。不要先要求实现整个新平台层或重跑全部历史验收。

希望输出：推荐方向、拒绝备选的依据、Core/平台的精确合同差异、source/frozen 影响、最小实验和实施顺序。明确区分静态判断、已有实测、推测与还需要的证据。

## 2. 两个问题的发现过程必须分开

| 问题 | 如何发现 | 已知影响 |
| --- | --- | --- |
| POSIX snapshot apply 被送入 Windows proof 路线 | 普通 Mac ResourcePackage 导入旅程 | 明确拒绝导入；不是先发生数据损坏才被发现 |
| 改回 legacy POSIX 路线仍缺连接/命名绑定 | 对候选修复的真实目录替换测试，随后独立 SQLite 实验 | 候选能向替换目录发布新 canonical 并返回成功；候选已撤回 |

当前已提交生产代码仍保持原先的安全拒绝。不能将下述“候选曾错误成功”描述成当前活动代码已部署该修复，也不能宣称所有 POSIX 数据库都已损坏。

普通失败链为 `PLATFORM.FS.CAPABILITY_UNAVAILABLE → ACTIVATION.RECOVERY_CAPABILITY_UNAVAILABLE → MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE → RESOURCE.IMPORT.RECOVERY_REQUIRED`。`TMMigrationService` 以 `_backend is not None` 选择 Windows portable 路线，但 POSIX backend 也不是 None。改成 None 又会丢失 root/W1/bound family，不能作为修复。

被拒绝候选把 POSIX 分派回 legacy owner 路线。普通 create/replace、receipt/callback/cold reopen 用例一度通过，但独立测试在真实 `_build_stage` 入口执行以下操作，并**继续真实 builder**：

1. 持有原资源目录和 W1；已有 canonical generation 为 0。
2. 把资源目录改名，在原路径复制一份同字节目录。
3. SQLite builder 随后按原路径打开替换目录；旧 retained root/W1 的复证仍可通过。
4. 替换目录可另取 W1，最终新 canonical generation 1 在替换目录发布并返回成功，原目录 canonical 不变。

早先“改名后立刻注入异常”的测试只验证异常处理，漏掉了继续运行真实实现的时序。这不是测试自造的成功结果；mock 只提供确定性的目录置换时机。该历史观察发生在 `07a12b7ed551fb495259dd9f6feec6414c467a6c` 加未提交候选；本文代码基线不改签原运行。原观察摘录见 [候选结果](review/posix-sqlite-binding/rejected-candidate-observations.json)，补丁和可选的隔离复核说明见 [被拒绝候选](review/posix-sqlite-binding/rejected-candidate/README.md)。

## 3. 机制：文件系统类别和发行形态是两条轴

POSIX 允许目录改名后，已打开目录 FD 仍引用原目录。`openat(retained_dirfd, basename)` 可继续访问原对象；普通 pathname 此时可能指向另一个目录。这是正常的系统语义，不是 POSIX 或 SQLite 的缺陷结论。

本应用有两个尚未闭合的义务：

- **实际访问绑定**：SQL 主文件、SQLite rollback journal，以及 Core 的 manifest/activation journal/backup/cleanup，不能重新从已漂移路径取得另一对象的权限。
- **成功时配置可达性**：最终 registry/configured locator 仍须指向本次发布的资源。在移走的旧目录完成写入，也不能自动称作用户配置下成功。

| 路线 | 本次影响 |
| --- | --- |
| Mac/Linux source | 受 POSIX TM 连接与恢复链影响；仅保持 source 并不能消除数据 I/O 问题 |
| Windows source 与现有普通 frozen | 使用相同 Windows 文件权威路线；禁止删除共享的目录句柄与 POSIX FD 模型不同。本次不能推导 Windows 同样发生了该反例，也不能把旧 EXE 当成未来新代码验证 |
| 未来 Mac/Linux frozen | 打包本身不会改变 POSIX 语义；当前不立项，只说明问题不能靠打包解决 |

若采用内部 native VFS，Mac/Linux source 多出构建/加载前提，但不必复制 Python/Qt 做自包含发行。Windows 分支应不加载该 POSIX 组件；共享 Core 变更仍须按实际影响复验。新驱动/VFS/锁/SQLite 编译选项等影响检索兼容性时，由 Core 决定资格失效和真实 Gate 重验；纯文档或 Git 变化不触发产品重验。

## 4. 独立 SQLite 负证据与边界

实测运行时：Python 3.14.7、SQLite 3.53.1、macOS 26.2 / Darwin 25.2.0、arm64。共 15 个原始实验，全部使用新建合成目录和真实 SQLite DELETE / FULL 事务。

| 机制或时序 | 观察 |
| --- | --- |
| 原生 openat + retained dirfd | 目录替换前后仍读原对象 |
| Mac `/dev/fd/<dirfd>/file` | 原生 ENOENT，SQLite CANTOPEN |
| `/dev/fd/<filefd>` | 可读已有 DB，DELETE 写事务失败；未证明具体哪个底层 syscall 拒绝 |
| 普通路径，连接前替换 | 提交成功，修改替换目录 DB |
| 普通路径，连接后但写入前替换 | `SQLITE_READONLY_DBMOVED`；只证明这个具体时序被拒绝 |
| 普通路径，INSERT 后、commit 前替换 | commit 成功；原 DB 被写，原 journal 遗留，替换目录的克隆 journal 被删除 |
| fchdir + 相对 SQLite URI | SQLite 形成命名路径；事务内替换同样出现 DB 与 journal 清理分离 |

最后一种事务内时序中，独立只读冷开原 DB 得到 `SQLITE_READONLY_ROLLBACK`。这证明 `DBMOVED` 或持有主 DB FD 并不足以覆盖 journal 生命周期；不证明任意环境/SQLite 版本都必然同样表现。

附带 [最小复现](review/posix-sqlite-binding/reproduce_sqlite_namespace.py) 只运行普通路径的无替换对照、连接前替换、INSERT 后替换三例，不导入 LocalCAT，不加载扩展、不安装依赖、不修改产品。用 `python reproduce_sqlite_namespace.py` 运行；[本次三例输出](review/posix-sqlite-binding/observations.json) 是观察结果，不签发能力或 PASS。实际产品反例还依赖被撤回候选，不能用这个独立实验代替其完整业务结论。

## 5. 不应混淆的合同与改进空间

- ResourcePortability `7.4/7.7/8.3` 要求 preview 绑定和首次 mutation 前复证；`8.4/8.6–8.7` 要求 TM owner 完整事务、失败保留/恢复或 recovery-required、成功冷重开；`12.3–12.4` 明列父目录替换及 prior 资产保留。
- ADR-020 要求业务消费共享 rooted/lock/publish 能力。其决策 8 明确 **lease + snapshot 不是 OS expected-identity CAS**；不能悄悄升级成抵抗任意非协作同用户进程的全部行为。
- Core `2.4/2.9–2.12/7.13–7.14` 保留完整 generation、来源绑定和失败恢复。现有 mutation-proof 段约束 replace/rename/delete；它没有提供一份已批准的 SQLite VFS 协议。
- POSIX v2 激活要求发布后仍是 sealed stage 对应 inode。当前 publisher 只接受自身创建的 CandidateFile；直接复制成新 candidate 会换 inode。需要评估受控的现有 stage 发布能力，或明确说明替代方案对持久合同的影响。
- Windows v3 journal/replacement 强依赖 `WindowsPrivateProof`、MAC 与 Windows 冷恢复规则。不能删除 proof 字段就把整条链当 POSIX 通用恢复。
- 不接受“最后返回 failure 即可，但可以在替换目录发布”作为修复；也不自动要求“任意攻击下所有瞬时内部写入绝对为零”。若认为现行承诺过强，请明确具体条文、真实用户损失和调整方案。

上述条文均可在代码基线的 `.kiro/specs/` 与 `.kiro/steering/adr/` 阅读；正式 RDT 和审批字段未因本次试验改写。

## 6. 备选方案，尚未定案

| 方案 | 可能收益 | 未解决的成本/风险 |
| --- | --- | --- |
| 现有接口内修复 | 最小依赖和维护成本，优先请外援挑战是否可行 | 尚无同时约束主 DB、journal 和完整发布链的具体机制；路径复证不能代替实际对象绑定 |
| 标准库 sqlite3 + 项目内部 C VFS | 宿主 `sqlite3_api_routines` 注册可保留同一 SQLite 引擎、exact Connection 类型和现有 SQL/异常 API | native 构建、受控内部扩展加载例外、完整有限 VFS、锁/临时文件/关闭生命周期；不是几行 xOpen wrapper |
| APSW + Python VFS | 成熟 Python/VFS 胶水 | 仍需自写文件和锁语义；官方 wheel 含独立 SQLite，不能只改写连接后与 stdlib 同 DB 混用；Core 驱动适配面扩大 |
| 内存 stage + serialize | fresh stage 可避开一部分文件路径 I/O | 未解决 canonical 写入与恢复；整库复制可能影响 100k / 512 MiB 内存合同 |
| 调整部分威胁/交付承诺 | 可能降低长期成本 | 必须明确取舍、保留用户数据边界并经正式裁决；不能把当前失败或静默降级包装成完整交付 |

**先前研究倾向内部 VFS，但这不是评审的预设答案。** 它仍缺真实 SQLite 接入原型；当前只有官方接口依据及有界 OS 能力观察。

锁方面特别容易漏项：传统 POSIX record lock 会因同进程其他 FD close 失效；同 SQLite 实例也不自动让自定义 VFS加入默认 unix VFS 的私有锁表。当前 macOS/APFS 的独立 [OFD 探针](review/posix-sqlite-binding/ofd-probe/report.md) 观察到不同 open 互斥、关闭无关 FD 不释放、与传统锁/新进程互斥；这不是完整 SQLite 锁协议验证。`dup` 共享 OFD 锁主体，多个连接不能靠 dup 同一个 FD 获得彼此互斥。最低 Mac 版本、Linux 和其他卷仍未验证。

## 7. 建议的有限决策顺序

1. 先确定最小威胁模型和数据结果，分别说明正常错误、合作并发、目录替换、非合作写入的保证。
2. 若现有接口足够，给出能覆盖已知反例的具体连接及 journal 机制，再做最小原型；不要凭“SQLite 很可靠”跳过本应用与其连接方式的接缝。
3. 若需要新增 VFS，先验证同一 stdlib 引擎注册、main/journal 一致绑定、实际 SQL commit/rollback/close、两个连接/进程的锁和故障状态。失败就停止该路线，不先迁移整个 Core。
4. 可行性成立后才冻结 Core/平台合同、native 构建依赖、保留格式或迁移边界，并进入正式 RDT 审阅与实施。
5. 最终完整交付包括 POSIX TM create/replace/常态读写/冷恢复、包与项目的跨设备旅程，以及实际受影响的 Windows frozen 回归。Mac/Linux frozen 不加入依赖图。

## 8. 代码导航与材料

关键文件：`tm_resource_port.py`、`tm_migration.py`、`tm_sqlite_store.py`、`tm_stage_sealer.py`、`tm_content_attestation.py`、`tm_activation_journal.py`、`tm_activation_recovery.py`、`tm_schema_upgrade.py`、`platform_fs_contracts.py`、`platform_fs_posix.py`。

发现范围内四个 Core 文件有 20 处文件 `sqlite3.connect` 和一处内存 probe；四处使用 exact `sqlite3.Connection` 检查。因此“只改一个中央 helper”不能自动覆盖所有 reopen、schema backup 和 sealer。具体导航见 [代码发现](review/posix-sqlite-binding/codebase-discovery.md)；外部 API、构建、锁和关闭风险见 [官方资料研究](review/posix-sqlite-binding/external-research.md)。这些是可反驳的发现输入，不是外援须服从的方案指令。

公开主要资料：[SQLite VFS](https://www.sqlite.org/vfs.html)、[rollback locking](https://www.sqlite.org/lockingv3.html)、[内部扩展入口和持续加载](https://www.sqlite.org/loadext.html)、[SQLite 锁与损坏风险](https://www.sqlite.org/howtocorrupt.html)、[Python sqlite3](https://docs.python.org/3.14/library/sqlite3.html)、[APSW 对比](https://rogerbinns.github.io/apsw/pysqlite.html)。

建议先阅读本文和复现结果，再定点查源码；无需上传全部环境、重新跑 100k、重签旧资格或恢复已经撤回的生产候选。
