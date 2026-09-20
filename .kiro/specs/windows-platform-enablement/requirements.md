# 需求文档

## 当前交付范围（普通 frozen 修订已批准）

Windows source 的平台适配、CPython 3.14 x64 专用 venv、源码与轻量 launcher 已完成 Task 6.6b 用户旅程。后置 frozen 追求最终用户无需另装 Python/Qt，独立验收，不阻塞 source。

[ADR-028](../../steering/adr/adr-028.md) 已批准普通桌面产品边界。用户明确批准 `reassessment@3e41130` 的下列 Requirement 10–12、对应 Design/Tasks 与已列明相邻 owner 修订，按现有依赖实施。Requirement 1–9 的 source 数据与业务合同、原 source/W3 构建前完成事实及证据范围保留。旧版本可从 `6531b1e` 追溯，不再把旧 W3 正文作为本路线的活动指令；本次审批不签发候选或正式 Gate 验收。

## 简介
立项时，`ui-mvp@b925b80` 因直接导入 `fcntl` 而无法在 Windows 运行。本 Spec 已通过平台端口与 consumer 适配闭合 source 的 Qt、项目持久化、TM 生命周期、TMX 和 FTS5；frozen 是下一条独立交付线。Windows 与 POSIX 使用各自的文件系统原语，保护相同的用户数据与业务结果。

## 边界说明
- **范围内**：Windows 原生 rooted file authority、路径逃逸与 reparse 防护、跨进程锁、原子发布与恢复；现有消费者的共享平台边界；Qt、项目、TM、TMX、FTS5；已完成的用户管理运行时与轻量 source 入口；普通 `--onedir --windowed` 自包含发行、受控构建可追溯性、真实 Core 消费、同候选业务验收及受影响的跨平台回归。
- **范围外**：旧 Excel 交互适配器、`xlwings` 或 Microsoft Excel 的安装与验证；`--onefile` 发行；安装器、自动更新、代码签名和商店分发；首版不承诺 remote/UNC share、FAT/exFAT 或不能证明 FileId/reparse/ACL/documented publish 前置的第三方文件系统，在这些位置必须明确 fail closed；当前版本不提供针对 storage controller/cache/power protection 或突然断电的硬件认证；改变翻译业务语义、TM 检索算法或 UI 产品流程；以 monkeypatch、mock、跳过 Gate、降低身份校验或现场修补代替能力实现。
- **相邻期望**：Feature 5 与 `feature5-ui-integration` 继续拥有 TM/UI 冻结契约；Parser、项目包、资源、TM snapshot/attestation/recovery 与协作分工规格继续拥有各自业务不变量和 consumer 接入实现；本 Spec 只拥有共享跨平台文件系统/锁合同、Windows backend、amendment dispatch/merge ledger、Windows packaging 和最终集成证据。

### Scope Lineage
- **Owning spec**：`windows-platform-enablement`；Governance owner 已批准其只拥有共享平台合同/backends、bootstrap/build、amendment merge ledger 与 Windows release evidence，并同步到 `.kiro/steering/spec-ownership.md`/`roadmap.md`。
- **被修订的既有范围说明**：`parser-subsystem-extraction/design.md` 中尚未落地的 Windows native rooted-handle 规划；`ui-mvp@b925b80` 仅在 POSIX 文件语义下可组合的现状。
- **相邻规格 / 契约**：当前真实 owning Specs 为 `feature5-ui-integration`、`parser-subsystem-extraction`、`collaborative-job-chunks`、`multi-document-project-workspace`、`language-resource-portability`、`tmx-context-interchange`、`tm-storage-retrieval-index`、`tm-store-module-extraction`、`termbase-column-selection-import`、`qt-editor-mvp`、`qt-editor-json-mvp-increment`；它们分别承载 collaborative、ProjectPackage/workspace、resource、TMX、TM store/activation/snapshot/attestation/recovery 与 Qt consumer contracts。另受 ADR-007/008/009/011/012/013/016/018/019 约束。
- **历史审批状态**：ADR-020～026、owning scope、原 WA-01～08 R/D/T amendment acknowledgement 及当时平台 Requirements/Design/Tasks 已批准；ADR-024/025 的后续取代关系由 Task 0.6 同步，ADR-026 对 Parser 发布状态的收窄由 Task 0.7 同步。它们不批准本轮普通 frozen 修订，也不代替实现或发布证据。
- **交付路线**：source 已完成用户管理 CPython 3.14 x64、venv、`requirements-ui.txt`、source 与轻量入口的产品验收；它不铸造 frozen authority。frozen 后置独立验收；2026-09-20 项目 owner 批准按 ADR-028 收窄其证明范围，取代原 W3 强证明作为唯一发行前置的安排。
- **导出输出收尾修订**：ADR-027 已获批准，仅修订 Windows ResourcePackage/TMX 正常完成后留下内部协调锁文件的行为；相邻 `language-resource-portability` 6.7–6.8 与 `tmx-context-interchange` 8.8–8.9 分别承载导出结果。既有 Requirement 3 的并发互斥继续成立，其他持久锁、直接 CSV/JSONL 导出与 POSIX 行为不在本修订范围。
- **本轮审批范围**：用户明确批准 `reassessment@3e41130` 的普通 frozen 活动 Requirements、Design、Tasks 及已列明相邻 owner 修订；优先进入平台 7.2a 与 Core 9.6e，再按既定依赖继续 Host 和生命周期整合。输入摘要与 fixture/contract 内容的区分、真实 Gate 消费及 worker 回收由正式任务验证；失败只修正具体接缝，超出批准边界时另提精确 delta。ADR-028 已足以承载边界，本轮不新增 ADR 或治理层，不重签历史验收。

## 需求

### Requirement 1：平台能力发现与安全失败
**目标：** 作为 Windows 用户，我希望 LocalCAT 能发现并组合 Windows 支持的能力，以便应用不会因 POSIX 专有导入而在启动前崩溃，也不会伪造安全能力。

#### 验收标准
1. When LocalCAT 在受支持的 Windows 11 环境启动, the 平台组合层 shall 不加载仅 POSIX 可用的模块或原语，并完成能力发现。
2. If 任一安全关键平台能力不能被当前主机证明, the 对应能力入口 shall 以稳定、结构化错误 fail closed，且不得读取正文、部分写入或发布新 authority。
3. While Windows 能力尚未就绪或验证失败, the LocalCAT shall 不通过 monkeypatch、mock、跳过 Gate、降低身份校验或平台分支常量宣称该能力可用。
4. The LocalCAT shall 保持现有 macOS/Linux 成功路径、错误码和 fail-closed 语义不回退。
5. The Qt 主程序 shall 不要求安装 `xlwings` 或 Microsoft Excel；仅旧 Excel 交互适配器可保留该可选依赖。

### Requirement 2：Windows rooted authority 与路径身份
**目标：** 作为安全边界维护者，我希望 Windows 文件访问绑定到已授权 root 和稳定对象身份，以便目录穿越、reparse 重定向或竞态不能逃逸授权范围。

#### 验收标准
1. When 调用方在已授权 Windows root 下请求读取或写入, the rooted authority shall 在使用期间持有足以证明 root、父目录和目标身份的原生句柄，并仅操作被证明位于该 root 内的对象。
2. If 任一路径组件是未获准的 symlink、junction、mount point 或其他 reparse point, the rooted authority shall 在读取正文或修改文件前拒绝请求。
3. If ancestor、final path、volume identity 或 live-handle file identity 在验证与使用之间发生替换或不一致, the rooted authority shall fail closed，并返回可诊断的稳定错误；跨重启 authority 不得仅凭可复用的 FileId 恢复。
4. While rooted 操作进行中, the rooted authority shall 不依赖当前工作目录、字符串前缀或仅一次 canonical-path 检查作为 containment 证明。
5. When 目标不存在且调用方获准创建文件, the rooted authority shall 证明目标父目录仍是获授权对象，并确保创建/发布不能跟随后来插入的重定向。
6. The Parser read/write 公共入口 shall 在 Windows 能力可用时保留现有业务响应，并在能力不可证明时继续返回 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE` 或经治理批准的更具体稳定子码。

### Requirement 3：Windows 跨进程互斥与 reservation
**目标：** 作为同时运行多个 LocalCAT 进程的用户，我希望同一资源的锁与 reservation 在 Windows 上真正跨进程互斥，以便不会产生双重 authority、交错写入或并发迁移。

#### 验收标准
1. When 一个进程持有某资源的排他锁或 activation reservation, the 另一进程 shall 无法同时获得同一资源的排他所有权。
2. When 持锁进程正常退出或被终止, the Windows 锁能力 shall 由操作系统释放该进程持有的锁，使后继进程能够在无人工清理的情况下继续。
3. If 锁对象、锁范围或底层文件身份不能被证明与目标资源一致, the 锁请求 shall fail closed，且不得进入受保护的发布或迁移区段。
4. While 锁已持有, the 文件打开共享语义 shall 允许协议要求的读取、flush 与原子发布，同时拒绝会破坏互斥和身份证明的冲突操作。
5. The Windows 锁合同 shall 不对公平性或排队顺序作超出操作系统可证明范围的承诺，并 shall 为竞争、超时、身份不匹配与平台不可用提供稳定结果。
6. When persistent lock file 首次由两个进程并发创建或 creator 在 payload durable 前退出, the 锁能力 shall 在`CREATE_NEW`返回`ERROR_FILE_EXISTS`后先用普通LOCK profile打开并复证：完整payload直接进入`LockFileEx`，该open的sharing violation才表示INIT进行中；空/严格前缀须关闭普通handle后争抢INIT share-none handle并二次复证再恢复。未知bytes/状态shall fail closed，且不得unlink/replace该载体。
7. When Windows ResourcePackage 或 TMX 导出已成功结束且不存在冲突占用或系统清理故障, the 平台输出收尾能力 shall 使输出目录不遗留该次导出的内部协调锁文件，且不改变最终导出文件。
8. If 本次导出结果不明、仍需恢复、协调文件无法安全确认或存在冲突占用, the 平台输出收尾能力 shall 不强行清理未经确认的文件、不扩大删除范围，且不因收尾失败把已经成功的导出改报为失败或把未确认的文件报成已清理。
9. When 新旧版本同时访问同一导出目标且旧版本因收尾竞争而未能开始操作, the Windows 锁能力 shall 明确失败并允许重新发起完整操作，且不得使两个进程同时进入该目标的排他写入区段。

### Requirement 4：原子发布、durability 与恢复
**目标：** 作为项目和 TM 数据的所有者，我希望 Windows 上的保存与发布在成功返回后满足已声明的持久化合同，并在中断后恢复到有效状态，以便不会出现静默丢失或半发布。

#### 验收标准
1. When 内容被提交到 canonical path, the 发布能力 shall 在同一授权 parent 中准备完整候选、完成内容 flush，并按 `CREATE_IF_ABSENT` 或持有 owner-defined 排他资源锁的 `REPLACE_UNDER_LOCK` 模式执行协议允许的原子命名操作；平台层不得把 pre-snapshot 宣称为 expected-target 原子 CAS。
2. When Windows 发布能力声明成功, the 实现 shall 满足 `WindowsDocumentedPublishV1`：Windows 11 x64、本地固定NTFS、rooted/reparse/live-identity与owner lease成立；candidate使用获批access/share/`WRITE_THROUGH` profile创建并写入，`FlushFileBuffers`成功，随后执行handle-bound原子命名、关闭candidate、reopen并保留destination，完成exact bytes/digest readback、owner state-machine commit与terminal reproof。
3. If arm之前任一所需Windows API、local fixed NTFS、security descriptor/reparse/FileId、flush/write-through或handle-bound naming能力不可用, the 发布能力 shall 返回`PLATFORM.FS.DURABILITY_UNAVAILABLE`并保持零命名mutation；If arm之后命名结果、destination reopen/readback、owner durable commit或terminal reproof失败或不确定, the 发布能力 shall 返回`PLATFORM.FS.RECOVERY_REQUIRED`，且恢复流程只接受完整且可验证的旧版本、新版本或recovery-only状态，不得消费部分文件。
4. While 读取句柄、锁句柄、临时文件或发布句柄仍存活, the 发布能力 shall 使用与原子替换协议一致的共享方式和生命周期；replace 后的 destination readback handle shall 保持禁止 write/delete 的共享约束，直到 owner durable metadata commit 与 terminal identity/digest/state reproof 完成，且不得自身阻塞合法 commit。
5. When replace、flush 或 recovery 失败, the 调用方 shall 收到稳定错误，现有 canonical authority shall 保持可识别，且失败候选不得伪装成已发布状态。

### Requirement 5：共享平台边界与现有消费者接入
**目标：** 作为维护者，我希望所有依赖 POSIX 文件语义的业务模块使用同一经过验证的平台边界，以便 Windows 修复不会散落成互不一致的特例。

#### 验收标准
1. When Parser、协作 chunk、项目包/工作区、资源保存、TMX 保存、TM attestation、snapshot 或 recovery 执行安全关键文件操作, the 模块 shall 通过受治理的共享平台边界获得 rooted、lock、replace 与 durability 能力。
2. If 任一业务模块仍直接依赖 Windows 不可用的 `fcntl`、`flock`、`dir_fd`、`O_DIRECTORY`、`O_NOFOLLOW` 或 POSIX 目录 fsync, the Windows 发布门 shall 失败。
3. While 平台实现不同, the 上层业务模块 shall 保持其既有 authority、atomicity、recovery、错误映射和 fail-closed 不变量。
4. The 共享平台边界 shall 为 Windows 与 POSIX 后端运行同一组合同测试，并允许平台专属反例补充验证。

### Requirement 6：Windows Qt 启动与基础运行时
**目标：** 作为 Windows 用户，我希望从干净环境启动 LocalCAT Qt 主程序，以便能够进入与 macOS/Linux 等价的 UI 工作流。

#### 验收标准
1. When 在干净 CPython 3.14 x64 venv 安装 `requirements-ui.txt`, the 依赖安装 shall 成功且 `pip check` shall 不报告依赖损坏。
2. When LocalCAT 以 Qt `windows` 平台启动, the 应用 shall 加载 `qwindows.dll`、创建真实可见主窗口并进入事件循环，而不是在业务模块导入或能力初始化时退出。
3. When 运行 LocalCAT 的受支持 smoke-test 入口, the 应用 shall 在无源码补丁、无环境 monkeypatch 的条件下返回成功并留下可验证结果。
4. If Qt 平台插件或必要运行时资源缺失, the 应用 shall 给出可诊断失败，而不是无提示终止。
5. When Windows avatar 功能使用有效的 speaker avatar catalog, the Qt UI shall 能索引并解码匹配头像；If 没有匹配头像, the UI shall 保持“— / 无内置头像”fallback。
6. When source compatibility 已闭合且用户安装 CPython 3.14 x64、创建专用 venv并按`requirements-ui.txt`安装依赖, the LocalCAT shall 可由轻量 Windows GUI入口使用受验证的绝对`pythonw.exe`与source bootstrap启动；该入口shall不复制或伪装bundled Python，不宣称 packaged/frozen release，并在环境或source失效时返回可诊断失败。

### Requirement 7：项目保存、重开与并发安全
**目标：** 作为译者，我希望在 Windows 保存并重开项目，以便译文、元数据和项目 authority 能跨进程重启可靠保留。

#### 验收标准
1. When 用户在 Windows 创建或编辑项目后保存, the 项目系统 shall 完成 canonical 写入，并在重新打开后恢复相同的受合同保护内容和元数据。
2. When LocalCAT 进程退出并重新启动后打开已保存项目, the 项目系统 shall 重新验证项目 authority，而不是依赖前一进程的内存状态。
3. If 项目路径经过 junction/reparse、ancestor swap、目标占用或跨进程竞争, the 项目系统 shall 遵循 rooted、lock 和 recovery 合同，且不得部分覆盖项目。
4. When 项目包使用 deterministic ZIP carrier, the Windows 路径 shall 保持 ADR-018/019 的成员安全、canonical 回读、LKG 和恢复不变量。

### Requirement 8：TM 激活、重启恢复与唯一 authority
**目标：** 作为使用翻译记忆库的译者，我希望在 Windows 激活 TM 并在重启后恢复，以便 canonical SQLite authority、generation 和 attestation 持续一致。

#### 验收标准
1. When Windows 上首次激活合法 TM source, the TM 系统 shall 序列化 bootstrap、发布唯一 canonical authority、返回公开成功结果并打开精确发布的 store。
2. When LocalCAT 重启后重新进入同一 TM workspace, the TM 系统 shall 恢复已发布 generation、验证 content proof，并避免重复 initial activation。
3. If 两个进程并发尝试首次激活同一 TM, the TM 系统 shall 最多发布一个 canonical authority；失败方 shall 获得稳定竞争/现有 authority 结果。
4. If activation 在 reservation、migration、seal、publish 或 cleanup 边界中断, the TM 系统 shall 按 ADR-012/013/016 分类残留与重新证明，不得把未知残留当作成功 authority。
5. If volume/file identity 或 device-local attestation 在 Windows 上不能被证明, the TM 系统 shall fail closed，并不得用弱于现有 POSIX 合同的路径字符串或可复制 token 替代。
6. When Windows TM authority 跨重启恢复, the TM 系统 shall 由 Gate D 或 canonical owner 在各自版本化 envelope 中重新证明当前对象、exact content digest、compatibility 或 generation/phase，并重验嵌套的 `WindowsPrivateProof` 与 device-secret binding；共享平台层 shall 不合并业务 envelope，持久 proof shall 不把历史 Volume/FileId 本身当作永久身份。W1 lock与W2 private对象shall按ADR-023采纳后的`ProtocolControlLockSecurityV2`/`WindowsPrivateSecurityV2`显式设置并handle-bound重验exact owner+DACL+medium mandatory-integrity label/`NO_WRITE_UP` projection；私有主体shall由process primary token的canonical `TokenUser` SID、token type、integrity、AppContainer/session与实际access facts表达，不得按账户provider、显示名、UPN或domain join状态分流。`WindowsPrivateProof.security_profile_id`与authority descriptor digest shall绑定同一V2 projection，V1/unknown profile shall fail closed。label读取shall在`READ_CONTROL` handle上使用`LABEL_SECURITY_INFORMATION`，不得要求完整`SACL_SECURITY_INFORMATION`/`ACCESS_SYSTEM_SECURITY`；audit ACE与mandatory label不得混同，DACL-only `AccessCheck`不得代答MIC。

### Requirement 9：TMX 导入、SQLite FTS5 与持久检索
**目标：** 作为译者，我希望在 Windows 导入 TMX 并通过 FTS5 使用已激活 TM，以便核心翻译记忆流程完整可用。

#### 验收标准
1. When 用户从获授权 Windows source 导入有效 TMX, the 导入流程 shall 完成 locale normalization、既有冲突规则和 canonical 写入，并报告准确导入数量。
2. If TMX source 逃逸 root、经过未获准 reparse 或在读取期间身份改变, the 导入流程 shall 在发布任何目标变更前 fail closed。
3. When SQLite runtime 声明 FTS5 可用, the TM 系统 shall 真实创建并查询项目所需 FTS5/trigram 索引，返回与既有排序合同一致的结果。
4. When LocalCAT 重启后查询先前导入并发布的 TM, the TM 系统 shall 从 canonical SQLite authority 恢复 FTS5 检索，不依赖一次性进程状态。
5. If FTS5 在发行运行时不可用, the TM 系统 shall 以稳定能力错误阻止依赖该能力的发布或激活，而不是静默退化为未经批准的检索语义。

### Requirement 10：普通 frozen 的输入、执行与能力资格
**目标：** 作为用户和维护者，我希望自包含发行物实际执行经过声明的产品与 Gate，并诚实展示当前设备可用能力，以便打包不会伪造资格或依赖开发环境。

#### 验收标准
1. When 构建和运行普通 frozen 候选, the 发行流程 shall 将 clean tracked 产品输入、固定依赖、owner 输入声明与实际产物关联，并使入口、独立 worker 和验收记录能够确认使用同一候选；构建记录本身不得授予业务能力。
2. When 用户调用 Matcher 或显式执行 Core Gate C/D, the 应用 shall 在该候选内消费实际所需的 contract、fixture 和实现信息并运行真实 Core；输入、执行和结果须属于当前运行，不读取 checkout 或外部 venv 代答。
3. If 必需输入缺失/不一致、候选混用、运行已撤销或结果不属于当前请求, the 对应消费者 shall 拒绝结果并保持安全状态，给出既有稳定诊断，不把失败表示为能力通过或普通无匹配。
4. The 普通 frozen shall 不以 frozen 标志、任意 PASS 文件、未执行的旁置源码或伪装 source/native authority 开放能力；不要求完整 Boot TCB、定制 native entry、外置源码逐字执行或第三方内部代码逐事件自证。
5. When 恢复本机 Fuzzy 资格, the Core shall 依据实际影响检索的实现、Gate/fixture、相关运行时、执行与测量机制及 intended path 判断兼容；无有效资格时只关闭 Fuzzy，保留各自已验证的 Exact/Context，允许用户显式重验而不在启动时自动运行 100k。无关 UI/头像改变不得无条件使资格失效，资源交换不得携带资格。
6. When migration/query worker 完成、失败、超时或用户取消/关闭, the 应用 shall 保持独立进程测量与既有计量口径、回收自己创建的 child 和端点且不阻塞 Qt 事件循环；取消先于提交胜出时不得安装过期结果，合法提交先胜出时须保留完整已提交结果并如实报告。

### Requirement 11：Windows onedir/windowed 发行物与资源
**目标：** 作为 Windows 用户，我希望获得可解压运行的 LocalCAT EXE，以便无需 Python 环境即可使用经过验证的功能。

#### 验收标准
1. When 构建 Windows 0.5.2 发行候选, the 构建流程 shall 提供普通 PyInstaller `--onedir --windowed` 产物，使用户无需另装 Python/Qt；onefile、安装器、签名和自动更新不在本次范围。
2. When 检查发行目录, the 产物 shall 包含实际 Python/Qt/SQLite 依赖、`qwindows.dll`、owner 必需 Gate contract/fixtures、`tm.jsonl`、`terms.csv`、logo、icon 与版本元数据；可选 avatar catalog 须有声明。发行物 shall 不要求旧 source-only 闭包、durability registry 或硬断电证据。
3. When 从非仓库当前目录启动 EXE 或传入项目参数, the 应用 shall 保持正常首页/打开项目行为，从发行目录取得只读资源，从用户目录取得可写配置与托管数据，且默认资源仅在缺失时播种、不覆盖既有用户数据。
4. The Windows 发行物 shall 使用受版本控制的构建配方、`.ico`、产品名称和版本元数据，并 shall 在运行时不消费构建机 checkout、外部 venv、CWD 或未声明的开发依赖。
5. If 必需资源、插件或声明输入缺失/损坏, the 对应入口 shall 给出可诊断失败且发行验收 shall 列出缺失项；未声明可选头像或无匹配时 shall 保留既有无头像 fallback。

### Requirement 12：端到端发布矩阵与可复现证据
**目标：** 作为发布审批者，我希望在干净 Windows 环境取得完整、可复现的通过证据，以便最终 EXE 的平台等价性可审计而非凭推断接受。

#### 验收标准
1. When Windows 发行候选进入最终验收, the 验证流程 shall 在同一实际候选上验证干净用户/无开发环境、非仓库 CWD、真实 Qt 首页/项目参数、项目编辑保存重开、TM 激活/重启恢复、TMX 直接导入、FTS5/trigram 创建查询重开、真实 Matcher/建议消费、资格恢复/失配，以及 worker 取消/关闭/超时/异常退出和新增资源加载路径。首次普通 packaged 资格及检索兼容性变化 shall 实际执行正式 Gate C/D 与 100k 双 intended paths；后续仅 UI/无关资源变化时，可由当前候选的 Core 核验并恢复同设备、兼容性未变的有效资格，记录原证据及复用依据，不重跑无关性能矩阵或重签旧结果。
2. When 验证候选的数据保护, the 验证流程 shall 在真实消费者中覆盖默认资源不覆盖、锁竞争、发布失败、中断恢复、权限/reparse 拒绝与既有数据不损坏；同时验证 TMX ResourcePackage 的 import/apply 负向拒绝及资源迁移不携带 Fuzzy 资格。更深入的底层故障矩阵按 12.5–12.7 确定重跑或复用。
3. If 任一候选必测或实际变更触发的阻塞项失败、跳过、缺少证据，或构建无法追溯 clean tracked 输入及实际依赖, the 发行状态 shall 保持 NOT_VERIFIED；source PASS、小样本、mock、临时补丁或不同 EXE 的成功片段不得代答候选验收。
4. The 验证交付物 shall 记录候选标识、构建输入—产物清单、环境版本、可复现命令、退出码/日志、逐项结果，以及复用 owner 证据的原锚点、不变前提和本候选消费检查；复用不得改写原证据或标成当前重新运行的 PASS。
5. When 复用未改变 owner 的深入单测/故障矩阵, the 发行评审 shall 核对实现、依赖、调用合同和关键运行条件均未改变且候选实际消费已验证；任一前提改变须重验受影响范围。Core/索引/Gate/运行时/worker 或计量变化须由 Core 判定对应资格和性能重验；共享数据端口变化须重验相关安全恢复矩阵；共享代码变化须运行受影响的 source/macOS/Linux 回归。纯 UI/头像变化只重验受影响功能与资源路径，不默认重跑全部平台和历史矩阵。
6. When 候选消费私有存储或本次改变 token/ACL/MIC 端口、调用合同或运行条件, the 验证流程 shall 在候选中证明实际私有存储消费，并对受影响部分重验 provider-agnostic standard/elevated 正向、同 SID low-integrity/restricted 负向及 service/AppContainer/impersonation 拒绝；未改变的深入矩阵仅在 12.5 前提成立时复用。domain/Entra 仍为可选覆盖，不构成环境强制门。
7. When 候选消费发布恢复或本次改变发布端口、owner 状态机、调用合同或运行条件, the 验证流程 shall 在候选中验证进程中断与应用冷重开，并对受影响边界重验 instruction fault、process termination、应用重启和正常 OS reboot 的 old/new/recovery-only 结果；未改变的深入矩阵按 12.5 复用。硬件掉电认证仍不在范围内。若修复改变候选，shall 更新候选身份、重验受影响项并对最终候选闭合 12.1–12.2，不能拼接旧候选的集成结论。
