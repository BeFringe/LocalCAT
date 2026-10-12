# POSIX TM 最小修复：范围、接线与审批包

本包续接外援 [`REVIEW-POSIX-SQLITE-BINDING.md@b3d5e26`](https://github.com/BeFringe/LocalCAT/blob/b3d5e26e76860ef6f673f1d6f63abb401d55210d/REVIEW-POSIX-SQLITE-BINDING.md)。生产内容基线仍是 `2e1cbce`。本轮只完成候选治理／owning R/D/T 差异、接线与验证计划；当前 POSIX ResourcePackage create/replace 的误拒绝尚未修复，跨平台资源交付仍未完成。

## 审阅顺序与当前状态

1. [ADR-033 草案](adr-033.draft.md)及[最小治理差异](governance-delta.md)：明确稳定本机目录和外部在线替换的不同保证，保留正常数据保护。
2. [owning R/D/T 差异](contract-discovery.md)：Core、共享平台、ResourcePortability、Feature 5 UI 四个 owner 的准确条款与替换文字。Sync/RPY 只保留原依赖，不新建另一条资源事务入口。
3. 下列具体接线与 [任务／验证清单](tasks-and-validation.md)；逐函数发现依据见 [wiring-discovery.md](wiring-discovery.md)。
4. [Git 集成方案](git-integration.md)：ADR 在治理线，获准开发栈在其后；精确区间共 225 个提交。

用户已经确定产品方向；本包具体 ADR、接口和 R/D/T 差异尚待按原流程审阅。正式 `.kiro`、审批 metadata、历史完成项及生产代码未改。本包中的任务都是候选，不是已实施事实。依赖设计审批后实施；不恢复已撤回候选。

[独立复审](plan-review.md) 已确认候选包可交用户审阅，首轮发现及修稿过程保留。该结论不批准正式规格、治理采纳、历史重放或实施，也不代表两个技术门已通过。

## 1. 收束后的支持边界

稳定本机目录下的正常 create/replace、query/append、导出、冷重开、LocalCAT 自己协调的并发／替换／崩溃恢复，以及包交换和退出后的正常迁移必须贯通。外部程序在线移动／替换活动 TM 家族不受支持；已可观察的漂移仍须拒绝，检测后停止该资源的新操作、保留恢复事实，不接管同路径副本。

目录检查不等于 SQLite 内部全部 I/O 绑定；不承诺检查间竞争、ABA 或活事务外部替换无副作用。恶意包、路径穿越、链接、摘要篡改、应用 rooted 发布、完整版本与诚实结果继续约束。配置 JSONL／manifest 的外部内容变化仍按原 `SOURCE_DIVERGED` 合同处理；canonical 与其位置仍可证明时，原 Lookup/Update 能力保留。正常操作失败不能被重新命名为“外部不支持”。

## 2. Boundary Commitments

| Owner | 持有的事实与职责 | 本轮具体文件 |
| --- | --- | --- |
| 共享平台 | root 配置位置关联、live file/lock、受控 stage 交接及命名、POSIX 旧 codec 所需的只读观察 | `platform_fs_contracts.py`、`platform_fs_posix.py`、`platform_fs.py`；Windows 只补共享接口兼容及原行为验证 |
| Core | 家族会话、admission/drain、SQL连接与raw FD顺序、codec选择、seal、generation、journal/receipt/恢复 | `tm_sqlite_store.py`、`tm_engine.py`、`tm_migration.py`、`tm_stage_sealer.py`、`tm_content_attestation.py`、`tm_activation_journal.py`、`tm_activation_recovery.py`、`tm_schema_upgrade.py`、`tm_snapshot_artifacts.py`、`tm_snapshot_recovery.py` |
| ResourcePortability | 包预检与preview，借同一Core owner事务，registry/receipt最终闭合 | `tm_resource_port.py`、`resource_portability.py`；`resource_repository.py`／`resource_receipt_ledger.py` 仅改实际受影响的终态借用，不改变格式 |
| Feature 5 UI/Application | runtime与ResourcePort共享本机资源会话，局部失败、晚到结果与独立项目结果 | `tm_application_composition.py`、`editor_tm_adapter.py`、`editor_controller.py`；Qt只有必要的既有失败说明投影 |

**Out of Boundary**：VFS、驱动切换、journal mode变化、新持久格式、全局资格系统、新平台层、原始 SQLite 同步、Mac/Linux frozen、检索算法重构与强对抗全时序保证。

**Allowed Dependencies**：Application→Core公开owner；Core→既有platform合同；adapter→OS。平台不导入 TM codec；业务不取得 raw FD、不解码 opaque identity。Sync只经既有包artifact及本机validate/preview/apply。

**Revalidation Triggers**：真实生命周期、兼容性执行输入、sealer／publication／recovery和共享接口的修改触发对应owner回归；Windows普通frozen按实际影响复验，Git父节点或文档变化不触发全量测试。检索语义和Fuzzy资格门不因本次边界改成无条件复用。

## 3. 具体接口与接线候选

以下是职责和前后条件，名称用于审阅，不新增通用框架。

### 3.1 一次选择恢复与 attestation profile

现有composition在构造资源owner时输出明确的 `POSIX_V2` 或 `WINDOWS_V3` profile；此值由受信组合决定，不接受包或UI指定。profile仅选择既有codec/流程，不能代替Windows真实private recovery capability。通用backend始终保留。

`_recover_existing_initial_activation`、initial journal/publish、`apply_snapshot_payload` replacement、StageSealer和受影响schema recovery消费同一profile。禁止以 `backend is not None` 推导Windows；禁止Windows capability失败后改走POSIX。全部Windows-specific narrow接口保留真实检查。

### 3.2 资源家族会话与操作借用

在既有Core coordinator/应用runtime组合中增加窄的家族会话：持有原root、原配置位置关联、资源与目标身份、资源级失效状态。runtime、ResourcePort及冷恢复入口消费同一会话，不能每次新建TMEngine获得新root清除旧失效。不同resource登记若指向同一物理家族，必须共用其协调边界或明确拒绝冲突，不能重复铸造互不知情的owner。

外层operation取得一次W1并签发不可序列化、绑定resource/operation/thread的借用；stage、seal、receipt、health、callback和recovery内部只借，不递归acquire。外层owner负责close，借用者不能提前关闭root/W1。源包有独立的只读输入authority，不成为目的资源授权；导出目标也保留自身publisher owner。

```mermaid
flowchart LR
    Entry[本机资源入口] --> Family[原资源家族会话]
    Family --> Admission[准入与排空]
    Admission --> Lease[同资源 W1]
    Lease --> SQL[受控 SQL 阶段]
    SQL --> Capture[关闭后观察与封存]
    Capture --> Publish[原 root 应用发布]
    Publish --> Terminal[复证与终态]
    Terminal --> Release[释放操作借用]
```

锁顺序：普通操作在短持condition期间计入active admission，释放condition后取得W1，再复核generation及原root并执行。替换先阻止新admission、排空本地lease，再取得W1；不能持W1等drain，也不能持condition等OS锁。借用覆盖懒query view、cursor和backup至关闭；有界等待/取消返回原busy/failure语义，并准确归还active count。跨进程W1取得后必须重验当前完整generation，不能把等待前的旧generation当成新库使用。

本候选按外层operation短时串行同一资源，其他资源独立，不永久单进程独占资源。该策略与正常重叠查询/取消的预算是否兼容，由有限实验验证，不能只靠W1单测作结论。

### 3.3 原 root 的配置位置复证

在既有POSIX adapter为资源绑定保留最初配置root与稳定祖先至root的名称链，派生的parent/file/lease继承该关联。可用窄操作 `root.reprove_location()` 表达：返回仅表示当前名称仍对应原live root；失败返回既有identity-stale/capability failure，不签发新root。

首次绑定前使用现有路径正规化与允许系统别名规则；此后不通过重新resolve/bind同路径刷新旧授权。合法入口如macOS规范化后的 `/private/var` 不因新检查被意外禁用。首次建立链本身必须保留no-follow与identity检查；不能只检查最末名称而忽略其祖先已被替换。

W1取得后、首次mutation前、每个真实SQL连接／事务入口、SQL阶段后、应用publication前后及业务success前按阶段复证。失效锁存到原资源会话，不由finally恢复READY。这里不新增持久root身份、不提供跨重启授权或连续名字冻结。

### 3.4 SQLite 与原始文件阶段

真实connection入口消费operation borrow与明确用途，内部按阶段复证、打开、配置和关闭，仍返回exact stdlib `sqlite3.Connection`给原SQL owner。已有DB/reservation使用禁止隐式创建的打开方式；fresh stage先由原owner排他创建。DELETE/FULL/FK及既有busy策略保留。不能把SELECT当底层绝不写入，cold read可能触发hot-journal恢复。

同一数据库存在SQL连接/statement/backup时，不开关用于hash/copy的额外原始FD；已持有的reservation或mutation guard句柄不得中途close。关闭SQL后，在仍持原W1与root的范围内采集内容／对象事实。命名后receipt SQL的明确交接见§3.6，不能提前关闭仍pending的publication，再用重新打开的文件冒充其retained destination。POSIX此顺序与Windows原shared-read profile分别接线，不机械互抄。

逐入口覆盖以 `wiring-discovery.md` 第6节为起点：builder/schema、store/query view、sealer、receipt、health/completed、schema双连接backup、旁路stage验证、cold open、snapshot/refresh。每个入口的实际借用来源和close归属必须在实现中闭合；不以中央helper修改代替全链路。

### 3.5 v2 proof 与同对象 stage 交接

保留 `LegacyPosixContentFileProof(device, inode, size, sha256)` 的现有codec。content owner增加借原root或已打开file的内部capture入口；不再为source/seal采集重新bind相同路径。

现平台值只有opaque identity，不能直接生成旧v2数字。候选窄接缝为POSIX可选只读observation：只接受本adapter从原root签发的live `BoundRegularFile`，以同一文件句柄重新验证父链、regular/single-link、内容与fstat，返回旧codec需要的四项观察数据。Core content factory据此构造既有proof。无rawFD、无任意Path输入、不解析opaque bytes、不要求Windows提供numeric inode；观察数字不是权限，也不是新跨重启证明。

平台对自己创建的 `MutableFileReservation` 增加单次转交能力：输入原reservation、同家族lease及预期内容；重新复证并同步后，转为同对象的publishable `CandidateFile`，接既有 `begin_publish`。不接受Path或裸identity收养；成功消费旧reservation，attempt/sealer不可再次清理它。pre-transfer失败仍由原owner持有；转交后由candidate/pending归属；命名后不明沿原recovery facts处理。

正常冷恢复中的既有stage不能冒称本次creator reservation：由原v2 recovery owner先核验journal/phase/内容/现root，取得受限existing-file authority，复用现retirement/publisher机制完成同对象交接。该恢复交接与fresh reservation是不同准入前提；接口只消费活authority，不能凭持久proof+Path收养。若现有机制不足，实验停止并报告，不能绕回裸rename。

### 3.6 DB 命名子阶段与 receipt SQL 的交接

当前 `PendingPublication` 的terminal必须使用原retained destination，并要求identity与内容仍等于preliminary。保留此合同，不增加detach/rearm，也不允许receipt SQL改变内容后重写preliminary。选定接线是利用v2现有 `DB_REPLACED` durable phase，把数据库命名与随后合法SQL变更分开；这需要ADR-033明确限定ADR-020决策9在此POSIX TM子阶段的含义，不能只以W1尚在为理由提前close。

1. `_replace_activation_database` 从原reservation单次交接的Candidate完成rooted命名，保留DB pending及SEALED字节readback。`advance_after_effect(DB_REPLACED)` 使用原root写入、同步并独立复证既有journal；DB对象与seal仍相同。此时只证明命名子阶段，coordinator仍为ACTIVATING，不可见新generation。
2. 在DB pending仍打开且SQL尚未开始时，经原root和canonical相对名调用既有 `guard_existing_for_mutation`。补齐其POSIX adapter实现：保留同一regular/single-link对象和原名称链，仅提供身份/位置观察，不宣称禁止外部rename。双方live identity必须相同；POSIX guard复证只对retained FD做fstat及rooted no-follow名称观察，不临时打开/关闭同inode的DB句柄。随后DB pending完成terminal、关闭，二者全部成功才准许SQL。guard持续持有同一对象，root/W1/activation handle均不释放，不能凭历史数字重新接管路径。
3. `_publish_activation_receipt` 消费这个borrow/guard；先复证root和guard，再打开已有DB，按原SQL元数据/receipt规则执行SEALED→ACTIVE。原事务内 `_capture_content_file` 的额外open/close前移到SQL前；SQL阶段用既有逻辑约束与持续guard，不能把删去raw检查说成消除了外部写入竞争。SQL结束后再复证并采集final内容，采集句柄与guard同时live比较对象；全部相关SQL关闭后才关闭guard。
4. manifest、MANIFEST_PUBLISHED、generation、terminal和registry仍按原状态机完成。DB命名terminal不等于activation成功，不使source pending提前结束；source/manifest等不变字节publication仍覆盖各自owner durable commit。Windows与其他消费者的pending生命期不变。

失败事实：DB_REPLACED尚未可靠持久化时保留最后PREPARED及实际命名事实，由原恢复owner处理；已写DB_REPLACED但guard、terminal或SQL失败时保留该phase，停止新操作，不伪造回到原库。receipt commit结果不明仍由既有DB_REPLACED恢复核验SEALED/issued或ACTIVE/completed窗口，不立即重写或删除日志。guard在SQL关闭/回滚收尾前不得close；外部漂移时不声称这些SQLite内部收尾无副作用。

冷恢复没有上一进程的live handle：先由现有v2恢复owner验证本次root/W1、journal、phase、source及完整DB closure，再取得本次有限guard；不得把原数值identity单独当授权。首个实验指定覆盖DB_REPLACED的receipt前和commit后/manifest前两窗口，重复恢复不增加generation。此方案不增加持久phase或codec。

### 3.7 应用发布、失败与UI

source/DB/manifest/journal/terminal/lineage/backup/cleanup的应用命名操作复用原root与publisher；只适配原v2 owner I/O接缝，不复制状态机。registry/receipt回调、最终cold reopen与成功投影在外层owner仍有效时闭合。create/replace不能在排空家族之前先改source。

检测漂移后停止新操作、撤销未提交结果；不路径重绑、盲目回滚或删除未知journal。SQLite owner仍结束自己的连接，但不声称rollback/close没有内部访问。既有failure/report字段表达prior可证、已发布、unknown/recovery-required；不制造持久“目录资格”。

UI只显示该资源不可用与安全恢复要求，拒绝晚到完成恢复旧状态；仍有效peer资源与项目编辑／保存继续。root失效不同于 `SOURCE_DIVERGED`，也不能进入ADR-016的device-only re-attestation或Fuzzy验证按钮。

## 4. 决策与审批边界

上述候选增加的是现有平台中的窄内部端口和Core生命周期接线，不是新平台层；但它们改变调用边界，必须随owning设计审阅，不能作为“局部实现细节”跳过。

首个技术门只做合成小数据的v2全链路与两coordinator／两进程生命周期实验。它们通过才能扩展全部reopen和恢复切点；失败时报告具体正常场景及接口缺口，不自动换VFS、永久单owner、copy stage或新持久格式。首发矩阵和残余边界分列于任务清单。

完整实现仍须完成正常POSIX旅程、资源级失效、Windows实际共享变化回归和RPY6.2/6.3交付依赖。这个审批包不签发其中任何一项PASS。
