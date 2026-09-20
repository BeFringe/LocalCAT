# 需求文档

## 当前交付范围（2026-09-20 修订）

Windows source 的平台适配、CPython 3.14 x64 专用 venv、源码与轻量 launcher 已完成 Task 6.6b 用户旅程。后置 frozen 追求最终用户无需另装 Python/Qt，独立验收，不阻塞 source。

[ADR-028](../../steering/adr/adr-028.md) 已取代普通 frozen 的完整 Boot TCB、定制 native entry 与 retained-source-only 强证明前置。本线保留 Requirement 10 的 W3 专用证明及 Requirement 11/12 对它的引用，用于强证明路线的独立续接，不直接驱动普通发行实现；自包含、可追溯构建、必要数据保护、Core Gate 与真实 packaged E2E 目标继续保留。新的输入身份/消费设计尚待收束，旧 W3 失败不改记为通过。Requirement 1–9 的 source 数据与业务合同保持有效。

## 简介
立项时，`ui-mvp@b925b80` 因直接导入 `fcntl` 而无法在 Windows 运行。本 Spec 已通过平台端口与 consumer 适配闭合 source 的 Qt、项目持久化、TM 生命周期、TMX 和 FTS5；frozen 是下一条独立交付线。Windows 与 POSIX 使用各自的文件系统原语，保护相同的用户数据与业务结果。

## 边界说明
- **范围内**：Windows 原生 rooted file authority、路径逃逸与 reparse 防护、跨进程锁、原子发布与恢复；所有现有 POSIX 文件语义消费者的共享平台边界接入；LocalCAT Qt 启动、项目生命周期、TM 生命周期、TMX 导入、FTS5；依赖用户管理 CPython/venv/source 的轻量 Windows 启动入口；PyInstaller `--onedir --windowed` frozen-source 能力证明、资源收集和 Windows 发行物验证；Qt speaker avatar 的 Windows 功能回归；macOS/Linux 回归保护。
- **范围外**：旧 Excel 交互适配器、`xlwings` 或 Microsoft Excel 的安装与验证；`--onefile` 发行；安装器、自动更新、代码签名和商店分发；首版不承诺 remote/UNC share、FAT/exFAT 或不能证明 FileId/reparse/ACL/documented publish 前置的第三方文件系统，在这些位置必须明确 fail closed；当前版本不提供针对 storage controller/cache/power protection 或突然断电的硬件认证；改变翻译业务语义、TM 检索算法或 UI 产品流程；以 monkeypatch、mock、跳过 Gate、降低身份校验或现场修补代替能力实现。
- **相邻期望**：Feature 5 与 `feature5-ui-integration` 继续拥有 TM/UI 冻结契约；Parser、项目包、资源、TM snapshot/attestation/recovery 与协作分工规格继续拥有各自业务不变量和 consumer 接入实现；本 Spec 只拥有共享跨平台文件系统/锁合同、Windows backend、amendment dispatch/merge ledger、Windows packaging 和最终集成证据。

### Scope Lineage
- **Owning spec**：`windows-platform-enablement`；Governance owner 已批准其只拥有共享平台合同/backends、bootstrap/build、amendment merge ledger 与 Windows release evidence，并同步到 `.kiro/steering/spec-ownership.md`/`roadmap.md`。
- **被修订的既有范围说明**：`parser-subsystem-extraction/design.md` 中尚未落地的 Windows native rooted-handle 规划；`ui-mvp@b925b80` 仅在 POSIX 文件语义下可组合的现状。
- **相邻规格 / 契约**：当前真实 owning Specs 为 `feature5-ui-integration`、`parser-subsystem-extraction`、`collaborative-job-chunks`、`multi-document-project-workspace`、`language-resource-portability`、`tmx-context-interchange`、`tm-storage-retrieval-index`、`tm-store-module-extraction`、`termbase-column-selection-import`、`qt-editor-mvp`、`qt-editor-json-mvp-increment`；它们分别承载 collaborative、ProjectPackage/workspace、resource、TMX、TM store/activation/snapshot/attestation/recovery 与 Qt consumer contracts。另受 ADR-007/008/009/011/012/013/016/018/019 约束。
- **审批状态**：ADR-020～026、owning scope、WA-01～08 current R/D/T amendment acknowledgement及平台Requirements/Design/Tasks均已批准；ADR-024/025对主体环境矩阵与发布耐久门的后续取代关系由Task 0.6同步，ADR-026对Parser发布状态的收窄由Task 0.7同步；实施仍仅按本Spec task依赖逐簇进入，审批不代替实现或发布证据。
- **交付路线**：source 已完成用户管理 CPython 3.14 x64、venv、`requirements-ui.txt`、source 与轻量入口的产品验收；它不铸造 frozen authority。frozen 后置独立验收；2026-09-20 项目 owner 批准按 ADR-028 收窄其证明范围，取代原 W3 强证明作为唯一发行前置的安排。
- **导出输出收尾修订**：ADR-027 已获批准，仅修订 Windows ResourcePackage/TMX 正常完成后留下内部协调锁文件的行为；相邻 `language-resource-portability` 6.7–6.8 与 `tmx-context-interchange` 8.8–8.9 分别承载导出结果。既有 Requirement 3 的并发互斥继续成立，其他持久锁、直接 CSV/JSONL 导出与 POSIX 行为不在本修订范围。

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

### Requirement 10：frozen-source 能力证明
**目标：** 作为能力 Gate 的维护者，我希望 frozen EXE 能证明它执行的安全关键源码和 fixtures 与受审版本一致，以便 PyInstaller 冻结不会绕过 source identity 契约。

#### 验收标准
1. When frozen LocalCAT 初始化 capability host, the frozen-source 合同 shall 为所有能力关键模块提供真实、严格存在的 `.py` 来源，并由获批 `TrustedSourceLoader` 从 retained verified handle 读取、摘要和直接编译 exact source bytes；loader metadata alone shall 不构成 executed-byte proof。
2. When Gate A/C 或其他能力图解析 root manifest, the frozen 产物 shall 包含递归闭包中的源码、JSON/TXT fixtures、摘要和相对路径，并执行与源码环境相同的 Gate。
3. If frozen 模块的运行来源、executed-source digest、loader attestation、manifest 闭包或 fixture 身份不一致，或关键模块存在 `.pyc`、`__pycache__`、未声明 PYZ duplicate, the capability host shall fail closed，且应用不得宣称相关能力可用。
4. While 构建 frozen 产物, the 构建流程 shall 不以仅把 `.py` 复制为 data、检查 `origin`/`co_filename`、隐藏导入或跳过 validation 的方式假定 source identity 已满足。
5. The frozen-source 合同 shall 同时说明源码运行与 frozen 布局的 bundle root 解析，并不得依赖构建机当前工作目录。
6. When frozen capability host 导入任何能力关键模块或读取 fixture, the release-owned native bootloader shall 审计 entry 前应用 PE import、在首次 Python DLL 或其他非系统 DLL load 前固定 DLL 搜索并绑定 bundle/native directory，递归枚举应用及随包非系统成员的 native static/delay-load closure，并要求 owner manifest 显式声明其 pre-authority 动态 native roots 与系统 API 入口；在任一非系统 DLL 可执行加载前逐个完成 retained-handle rooted/reparse/live-identity/digest 预证明。加载后还 shall 把实际非系统 module 与预证明 handle 复核；未声明应用动态 load、无法闭合的应用依赖或仅依靠顶层 flags/post-load path check 均 shall 使 spike fail closed。随后 native bootloader 移交不可伪造 attestation；Python bootstrap 再闭合版本化应用 Boot TCB（bootloader、Python DLL、pre-authority hooks、必要 stdlib/ctypes/hash/manifest/loader 与随包 native 成员），以不依赖待验证 Python adapter 的原生可信根证明 ancestor/final reparse、live-handle identity、manifest digest 和 handle-read。system allowlist shall 约束应用系统 API 入口与解析策略。不得先信任 capability host 再用其证明自身，也不得声称 Python-level policy 能追溯保护 entry 前加载。

### Requirement 11：Windows onedir/windowed 发行物与资源
**目标：** 作为 Windows 用户，我希望获得可解压运行的 LocalCAT EXE，以便无需 Python 环境即可使用经过验证的功能。

#### 验收标准
1. When 构建首个 Windows 发行候选, the 构建流程 shall 使用 PyInstaller `--onedir --windowed`；切换到 `--onefile` shall 视为改变 frozen trust/recovery boundary，并须另立 ADR 后方可实施。
2. When 检查发行目录, the 产物 shall 包含 `qwindows.dll`、获批 frozen-source 闭包、Gate fixtures、`tm.jsonl`、`terms.csv`、`LocalCAT-logo-silver.png`和`benchmark_tm_contract.json`；发行物shall不要求或消费`durability_profiles.json`或forced-power-loss evidence，其他必须输入缺失或tamper时仍不得mint相应capability。
3. When 从非仓库当前目录启动 EXE, the 应用 shall 通过 bundle root 解析并加载所需数据和资源。
4. The Windows 发行物 shall 使用受版本控制的 `.ico`、产品名称和版本元数据，并 shall 不从构建机仓库路径读取运行时依赖。
5. If 任何必须资源、插件或 source-proof artifact 缺失, the 发行验证 shall 失败并列出精确缺失项。

### Requirement 12：端到端发布矩阵与可复现证据
**目标：** 作为发布审批者，我希望在干净 Windows 环境取得完整、可复现的通过证据，以便最终 EXE 的平台等价性可审计而非凭推断接受。

#### 验收标准
1. When Windows 发行候选进入验收, the 验证流程 shall 从干净用户配置执行依赖/构建、Qt 启动、项目保存/重开、TM 激活/重启恢复、TMX 导入、SQLite FTS5、qwindows、全部能力 Gate、锁竞争、崩溃恢复和资源可见性测试。
2. When 运行安全反例矩阵, the 验证流程 shall 覆盖 symlink/junction/reparse、hardlink、ancestor swap、目标占用、进程终止、双进程竞争、替换失败和恢复边界。
3. If 任一发布阻塞项失败、跳过、只在源码环境通过，或 production build 不能证明 clean tracked tree 与全部实际构建输入的 content-addressed provenance, the Windows 发行状态 shall 保持 NOT_VERIFIED，且不得发布为可用版本。
4. The 验证交付物 shall 包含可复现 PowerShell 命令、环境与版本清单、完整日志、通过/失败矩阵、Windows 文件系统/锁适配清单和 frozen-source/打包清单。
5. The 验证流程 shall 在同一提交上运行 Windows 与既有 macOS/Linux 回归，并明确记录平台专属预期差异。
6. The Windows blocking矩阵 shall 对主体使用provider-agnostic process-primary token shape：standard/elevated正向、同SID low-integrity/restricted负向，以及service/AppContainer/impersonation fail-closed为mandatory；domain/Entra环境只可作为optional非阻断覆盖，缺少这些环境不得构成skip或NO-GO。
7. The Windows publish blocking矩阵 shall 覆盖process termination、instruction-boundary fault、应用重启与正常OS reboot后的old/new/recovery-only结果；storage bus/controller/cache/power-protection枚举和forced-power-off/power-cut不得作为source、frozen、runtime或最终GO前置。
