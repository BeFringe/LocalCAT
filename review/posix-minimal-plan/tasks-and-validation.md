# 候选任务图与验证清单

以下任务追加到各 owning `tasks.md`，不修改既有完成勾选或历史结果。编号是本轮候选；正式写入前再次核对冲突。每个任务的实现、定点验证和本owner任务状态形成同一语义提交；依原owner累计评审节奏，不为每个机械步骤新建评审制度。

## 前置条件

ADR-033、治理同步、四个owner的范围与设计差异以及精确Git重放方案分别经对应审阅后进入实施。此处不把尚未批准的决策伪装成已完成开发任务。治理闭合的可观察条件为：新开发基线继承唯一治理提交，四个owner的实际delta与审批范围相符，旧证据/引用保留。

## 1. 治理闭合与共享平台

非并行前置闭合项由治理集成人承担：消费实际已完成的ADR及精确重放授权，依Git方案形成唯一治理提交，验证新开发基线继承它；四owner的正式R/D/T差异分别完成原流程审批后才开放其实施任务。完成证据为实际祖先关系、范围diff与对应审批，不是新增永久台账，也不以该项代替人的决定。未获准重放时不移动受保护引用。

共享平台追加 Task 11：

| Task | 边界与依赖 | 可观察完成条件 | Requirements |
| --- | --- | --- | --- |
| 11.1 绑定原root与配置位置关联 | POSIX adapter/contracts；治理闭合 | 原root改名＋同字节副本被位置检查拒绝；祖先、link及合法规范化入口正确；派生authority继承原关联，Windows原语义不变 | 5.1, 5.3, 5.6, 5.7 |
| 11.2 为旧POSIX内容codec提供已绑定对象观察 | 同adapter/contracts；11.1 | 从原live file得到dev/inode/size/digest；foreign拒绝，无rawFD、opaque反解或新持久schema | 5.1, 5.6, 5.7 |
| 11.3 将同对象stage交给现有publisher | 同adapter/contracts；11.1，Core交接设计已批准 | fresh reservation单次转Candidate；同对象发布，转交失败/close归属明确；cold-recovery existing authority不能由任意Path伪造 | 5.1, 5.3, 5.6, 5.7 |
| 11.4 补齐POSIX现有mutation guard | 同adapter/contracts；11.1 | 与pending同live比较身份；只fstat＋rooted no-follow观察，不额外开关DB FD；SQL期间持续持有，位置失效拒绝，不宣称阻止外部替换 | 5.1, 5.3, 5.6, 5.7 |
| 11.5 复验实际共享变化的Windows普通frozen | 平台release owner；本表前项、Core10.11、Resource6.2、UI10.2 | 受影响root/锁/连接/发布和资源旅程在Windows source及普通frozen符合原合同；新候选与旧发行证据分开 | 1.1, 1.2, 5.1, 5.2, 5.3, 5.4, 5.5, 5.7, 10.1, 12.1, 12.2, 12.3, 12.4, 12.5, 12.7 |

平台不解释SQL、v2 journal或业务phase；Core决定何时可交接，平台只接活authority并约束单次转移、名字和对象。11.5不重新勾选或改写原0.5.2 Task10.1发行裁决。

## 2. Core：新增 Task 10

前六项把两个有限门实际需要的接线放在门前；其余项扩展消费者和切点。任何中间项完成都不等于生产资源功能已贯通。

| Task | 边界与依赖 | 可观察完成条件 | Requirements |
| --- | --- | --- | --- |
| 10.1 实现家族会话与显式操作借用 | coordinator/engine；平台11.1 | admission/drain/W1顺序无反转，内部借用不重入，资源失效不能被新engine清除，同物理家族冲突统一协调或拒绝 | 2.10, 4.7, 7.15, 7.16, 7.17 |
| 10.2 接入并验证生命周期的真实窄切片 | store/query view/health＋replacement reservation的Core集成；10.1 | 一个真实store的短query/append、lazy view、嵌套health接入borrow；双coordinator/双进程及timeout/cancel通过下述生命周期门，active count/句柄闭合 | 2.10, 4.7, 7.5, 7.15, 7.16 |
| 10.3 接入POSIX分派、builder与v2 seal | migration/sealer/content；10.2、平台11.2/11.3 | 正确profile覆盖真实builder/schema初始化、阶段写入和seal；原reservation同对象交接，格式不变，SQL结束后才raw观察；Windows仍使用真实private capability | 2.4, 2.9, 2.12, 7.14, 7.15 |
| 10.4 接通DB命名与receipt SQL交接 | activation recovery的DB I/O/receipt端口；10.3、平台11.4 | 真实DB_REPLACED journal持久复证→live guard交接→pending terminal/close→SQL→final capture；命名terminal不报activation成功；交接失败保留真实phase | 2.4, 2.9, 2.11, 2.12, 7.14, 7.15, 7.16 |
| 10.5 接通正常activation其余应用发布 | 既有activation journal/recovery I/O端口；10.4 | fresh create及已有generation replace必经的source/manifest/journal/lineage/backup/terminal都借原root；完整正常Core调用能走到底；排空前不改source，失败不裸路径清理 | 2.4, 2.9, 2.11, 2.12, 7.13, 7.14, 7.16 |
| 10.6 接入DB_REPLACED恢复并验证v2窄旅程 | 现有v2恢复owner＋Core集成；10.5 | receipt前、commit后/manifest前两个窗口经真实cold recovery闭合；重复恢复不多增generation；create/replace→receipt→cold query完成下述v2门 | 2.4, 2.9, 2.11, 2.12, 7.14, 7.15, 7.16 |
| 10.7 扩展其余runtime连接与失败入口 | store/engine；两门通过 | 余下query/append/health/reconnect实际入口继承借用；缺失DB不隐式创建，取消与晚到结果不复活失效；正常SOURCE_DIVERGED能力保留 | 2.10, 2.13, 4.7, 7.5, 7.8, 7.9, 7.10, 7.11, 7.15, 7.16 |
| 10.8 接入schema重开与双连接backup | schema owner/store ports；10.7 | schema copy、backup源/目标及旁路stage检查生命周期闭合；SQL前后顺序正确、cold recovery使用原owner，不新增格式 | 2.9, 2.11, 2.12, 7.14, 7.15, 7.16 |
| 10.9 扩展其余activation恢复和收尾切点 | activation recovery owner；10.6、10.8 | PREPARED、MANIFEST_PUBLISHED、GENERATION_PUBLISHED受影响恢复/rollback端口借原root；完整旧/新或recovery-only，漂移后不盲删/接管 | 2.4, 2.9, 2.11, 2.12, 7.13, 7.14, 7.16 |
| 10.10 接入snapshot/export/refresh | 原snapshot artifact/recovery owner；10.7、10.9 | 来源家族与目的root分开，导出完整快照；refresh/receipt/cold recovery闭合，未完成输出不覆盖有效输出 | 2.7, 2.8, 7.15, 7.16, 7.18 |
| 10.11 闭合正常并发恢复与可见漂移矩阵 | Core集成验证；10.7–10.10 | 真实builder已换目录时继续实际代码并拒绝错误访问；正常并发、受影响进程中断/重复恢复闭合；活事务外部替换另报边界观察 | 2.4, 2.9, 2.10, 2.11, 2.12, 7.13, 7.14, 7.15, 7.16, 7.17, 7.18 |

10.3–10.6是同一既有状态机的窄接线切片，不复制状态机。各项只修改命名的端口与直接调用者；两个门不依赖10.7以后的消费者扩展，也不能用门后尚未完成的旧pathname路线、mock提前抛错替代真实入口。

## 3. 资源交换与UI

| Owner / Task | 边界与依赖 | 可观察完成条件 | Requirements |
| --- | --- | --- | --- |
| Resource 6.1 贯通POSIX包create/replace及终态 | Resource/TM端口集成；Core10.6/10.9/10.10 | 先家族准入再source发布，借同owner闭合registry/callback/receipt/final；不新建engine绕过失效，不复制live SQLite | 7.7, 8.3, 8.4, 8.6, 8.7, 9.4, 9.6, 11.1, 11.2, 11.3 |
| Resource 6.2 验证包安全、正常失败与漂移分类 | Resource测试；6.1、Core10.11 | grammar/路径/链接/摘要/计数和preview stale拒绝保留；source包不变，prior/已提交/不明事实与receipt一致 | 7.7, 8.6, 8.7, 12.3, 12.4, 12.7 |
| UI 10.1 共享资源会话并投影局部停止 | Application composition/adapter/controller；Core10.1/10.7、Resource6.1 | runtime/port共用原资源owner；晚到结果/刷新不恢复旧授权；peer资源与项目编辑保存继续，无新验证按钮 | 5.6, 5.9, 6.5, 6.6, 6.14 |
| UI 10.2 验证资源停止与独立项目结果 | UI/controller测试；UI10.1、Resource6.2 | TM写回失败与项目保存成功分别呈现；不能以legacy回退、device-only重证或Fuzzy完成绕过失效 | 5.6, 5.9, 6.5, 6.6, 6.14 |

跨设备交付仍以RPY6.2/6.3为出口；Sync只复验原一致包入口，不能将本次设计完成改成同步前提已通过。

## 4. 两个有限技术门

对应设计/任务批准后运行；只决定能否扩展实现，不是产品交付PASS。

| 技术门 | 已前置的真实切片 | 通过条件 | 失败退出 |
| --- | --- | --- | --- |
| 家族生命周期门 | Core10.1/10.2；真实短query/append、replacement reservation、嵌套health和lazy view；双coordinator/线程、双进程、timeout/cancel | 同家族外层W1无递归/自等drain；旧generation不写新DB；raw close不破坏SQL锁；等待有界且count/lease可归还 | 报告正常并发场景和等待预算，不改永久单owner、不跳过路径，不向未验证机制扩展所有消费者 |
| v2同对象门 | 平台11.2–11.4、Core10.3–10.6；真实Core fresh create/replace必经I/O，DB_REPLACED两个指定恢复窗口 | 原root/W1连续，旧codec不变，stage/final同对象；pending terminal后guard覆盖SQL，冷重开与重复恢复正确，clone不被接管 | 指出proof、交接或正常恢复的具体缺口，保留实际阶段；不copy冒充stage、不Path收养、不重启旧候选或自动转VFS |

使用少量合成记录，不先跑100k，不操作用户游戏和日常TM。两个门的真实入口接线由上述任务显式承担；通过后才扩展剩余入口及完整中断矩阵。全部步骤仍须按既有owner实现评审执行，不以候选计划代替审核。

## 5. 完成交付的最小验证矩阵

| 范围 | 需要的实际证据 |
| --- | --- |
| Mac/Linux source 正常资源链 | 两平台各走实际Application→TM owner完成create/replace→query/append→导出→独立进程冷重开；覆盖托管目录及已登记的source/default资源路径，不静默搬迁原资源 |
| 配置源内容分歧 | JSONL／manifest内容变化仍按SOURCE_DIVERGED处理，canonical及其位置有效时保留原Lookup/Update；不与目录失效混同 |
| 协作和中断 | query/append与replace排空、双进程竞争、timeout/cancel；PREPARED、DB_REPLACED、MANIFEST_PUBLISHED、GENERATION_PUBLISHED实际变化切点；正常hot journal、schema backup及重复恢复 |
| 已可观察的绑定失效 | 在真实builder入口完成rename＋同字节clone并继续真实代码；另测首次mutation、reconnect、应用publication和终态前；检查两目录实际内容及callback/registry/receipt，不用立即抛异常替代 |
| 保留输入保护 | 恶意包、路径穿越、symlink/hardlink/special file、摘要/计数、preview stale、foreign候选拒绝；Resource导出及术语发布保留原合同 |
| UI局部影响 | 该资源新查询/写回/导入拒绝；晚到完成不恢复；其他资源、项目编辑保存继续；不出现目录资格按钮 |
| 跨设备包与退出后迁移 | Windows普通frozen和POSIX source导出/导入同一逻辑包，目标获本地身份并正常使用；不带live SQLite/journal或设备资格；离线重定位经原recovery核验，不新增raw家族迁移工具 |
| Windows实际共享变化 | source及普通frozen的相应root/锁/连接/发布/资源链回归；POSIX观察端口不在Windows铸造数值身份；新候选结果与旧发行结果分开 |

现有测试的具体复用落点见 [owning差异第7节](contract-discovery.md#7-最小验证清单与已有落点)。不把旧未提交测试或历史候选的通过作为当前能力证明。

## 6. 残余边界实验

SQLite活事务内外部目录替换使用独立进程注入，保留原／新主库、journal和commit/rollback/close实际观察。检测后的停止、不接管及不虚假成功仍检查；不要求所有字节无副作用，不将结果改写为正常产品PASS，也不自动升级成首发阻断或VFS立项。

历史实验及其环境/源码SHA保持不变；本轮新的观察另记实际环境。无事故频率数据时不编造“风险很低”。
