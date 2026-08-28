# Research & Design Decisions

## Summary
- **Feature**: `windows-platform-enablement`
- **Discovery Scope**: Complex Integration / Brownfield Platform Port / Frozen Distribution
- **Baseline**: `ui-mvp@b925b803d81001f55dea46ace8b26159ca82db19`，Windows 11、CPython 3.14.7 x64；本节矩阵是可移植的 spec-local baseline 摘要，原始现场报告只作 discovery 输入，不以本机绝对路径作为发行 evidence identity。
- **Key Findings**:
  - UI 依赖、PySide6/Qt、`qwindows.dll`、真实 Qt 窗口和 SQLite FTS5/trigram 均已通过；当前阻塞不是依赖安装或 Qt 插件。
  - 源码启动被 `collaborative_chunk_store.py` 顶层 `import fcntl` 阻断；Parser 之后又会按合同返回 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE`。静态扫描命中 43 个源码/测试文件，说明这不是单文件兼容补丁。
  - Windows 的 share mode 本身是协议一部分：缺少 `FILE_SHARE_DELETE` 会阻止 rename/delete；这既能固定 rooted ancestor，也会在错误的 handle lifetime 下阻塞合法原子发布。
  - Windows 的 `VolumeSerialNumber + 128-bit FileId` 只适合比较同时存活的 handles；FileId 删除后可能复用。W2只提供nested `WindowsPrivateProof`，Gate D compatibility与canonical phase/generation继续由各自owner envelope绑定。ADR-024把主体环境门收敛到process-primary token/SID facts；ADR-025以`WindowsDocumentedPublishV1`固定write-through、flush、handle-bound naming与recovery边界，不再以provider来源或storage硬件profile铸造authority。
  - PyInstaller可把指定模块以真实`.py`外置收集，但`SourceFileLoader` metadata仍可能对应bytecode cache；W3必须用retained-handle exact-byte loader/direct compile证明实际执行bytes，并完整列出bootstrap TCB，不能只复制data或检查origin/co_filename。

## 现场失败/通过矩阵

| 能力 | `b925b80` Windows 现场结果 | 差距性质 | Spec 排期含义 |
|---|---|---|---|
| `requirements-ui.txt` / `pip check` | PASS | 无依赖闭包阻塞 | 作为每个发行候选的前置证据保留 |
| SQLite FTS5 + trigram | PASS | 运行时已有能力 | 冻结后仍须重新创建/查询，不能沿用 venv 结论 |
| venv `qwindows.dll` + 可见 Qt 窗口 | PASS | Qt 基础可用 | 打包阶段验证目录布局和真实窗口 |
| LocalCAT source smoke | FAIL：`No module named 'fcntl'` | 组合层导入阻塞 | 第一实现簇先建立平台边界并恢复启动 |
| Parser rooted capability | FAIL：能力缺失 | 安全基础设施缺失 | 文件身份/containment ADR 与后端必须先于业务修复 |
| Parser 缺失能力失败语义 | PASS | fail-closed 正确 | 全程保留为 unsupported/failure 结果 |
| Windows open-file / byte-lock 特征 | PASS：占用阻止 replace；跨进程 byte lock 有效 | 证明 share/lock 可实现但语义不同 | 先锁定 handle/share 合同再迁移消费者 |
| 项目保存/重开 | FAIL：root binding | 下游依赖失败 | 平台后端完成后作为第一条业务 vertical slice |
| TM 初次激活 | FAIL：`MIGRATION.INITIAL_AUTHORITY_UNAVAILABLE` | reservation + rooted/durability 缺失 | 锁/attestation ADR 后实施 |
| TMX 导入 | FAIL：导入 0、目标不变 | Parser rooted 依赖失败 | Parser + canonical writer 接通后实施 |
| PyInstaller onedir/windowed 构建 | PASS（仅构建） | 构建工具可用 | 定制 spec/hook，不把 build exit 0 当可运行 |
| frozen raw `.py` / fixtures / data | FAIL：均未收集 | frozen-source 组合缺失 | 生成闭包和 source collection；执行真实 Gate |
| frozen qwindows | PASS | 自动 hook 有效 | 最终发行仍执行可见窗口探针 |
| frozen LocalCAT 启动 | FAIL：`capability_host.py` 不可见 | source identity 失败 | frozen-source 是发布阻塞项，不延后为安装器问题 |

### Implement Notes — Task 1.1 fresh replay（2026-08-26）

- 在detached clean `b925b803d81001f55dea46ace8b26159ca82db19`与新建CPython 3.14.7 x64 venv中重放后，上表的依赖、source、Parser、Project、TM/TMX和clean frozen-source结论仍成立；portable evidence位于`artifacts/windows/b925b803d81001f55dea46ace8b26159ca82db19/20260826-task1-1-fresh-01/`。
- 新增信息：若PyInstaller继承包含外部tool runtime的`PATH`，其native analysis可把非CPython/PySide6来源的ICU/OpenSSL复制进dist，本次负向control因此使`Qt6Core.dll`以WinError 127失败；逐文件SHA-256相同的短路径副本排除了路径长度。净化为venv/standalone CPython/Windows system目录后，clean build恢复到既有`capability_host.py`不可见失败。因此W3 build provenance/native closure必须显式排除ambient `PATH`输入，不能只要求clean venv。
- Task 1.1旧baseline matrix中的`frozen.qwindows_collection=PASS`是aggregate visibility命令内的独立子事实；该命令因raw `.py`/fixture/resource缺失整体exit 1。两者不矛盾，但旧matrix的单一`exit_code`字段不够清晰；Task 1.2版本化scenario contract已把command exact exit与各scenario/event fact分离，后续release matrix不得沿用该歧义表示。

### Implement Notes — Task 1.2 portable evidence harness（2026-08-26）

- 三个scenario contracts已分别经真实system Windows PowerShell 5.1 success、expected-failure与timeout/interruption做pre-commit dry-run；这些bundle只证明producer与validator内部一致，不能对尚未tracked的合同/runner宣称外部三锚，也不预填会随候选commit变化的artifact摘要。cluster退出条件是唯一候选提交后，在不再改动tracked tree的前提下由clean orchestrator对该HEAD重放三lane并生成独立external-anchor records；任一未完成或失败时不得退出C1S/进入Task 2。
- `run_windows_evidence_harness_smoke.py`只生成脱敏、仓库相对identity的schema/日志证据并标记`INTERNAL_CONSISTENCY_ONLY`；`anchor_windows_evidence_harness_smoke.py`先验证HEAD/clean tracked orchestrator与`git show`合同bytes，再独立计算生成后的manifest摘要、执行外锚validator并写`EXTERNALLY_ANCHORED`记录。Task 1.1的完整build/dist、Analysis和原始现场日志继续保存在其content-addressed artifact key并由checksum ledger绑定，不进入Git或充当私有绝对路径authority。

### Implement Notes — Task 1.4 W3 stock feasibility（2026-08-26）

- 以CPython 3.14.7 x64、PyInstaller 6.22.2、官方sdist SHA-256 `89b65a3ad07d9dd5832253e37bc45f31872d10d7f9d5c9fd0fdd6088a83829dd`重放stock windowed bootloader静态门；`runw.exe` SHA-256为`87b0c589906a5d690c26c602bf2ee7e43b3eab55574bf72887a8ca07fbb2dba0`，baseline EXE为`20fccf54845e8b171928d5ed5f85efd716c5dfeb49508c0dcaf2ca0f163a5ac7`。工具直接比较sdist与extracted 51-file source aggregate，并以Task 1.1 checksum/matrix/inventory/Analysis把baseline绑定到`b925b803d81001f55dea46ace8b26159ca82db19`；stock/baseline `.text` SHA-256均为`6fcaee6735d961a7ded1c1d8fd789ef6d4a8ac7a5c551b04afbad8e32c1da4e3`。任一pin或关系不匹配为`INVALID_INPUT`/exit 2，不生成stock结论。审计器与机器可读结果位于`tools/audit_w3_stock_bootloader.py`和`artifacts/windows/b925b803d81001f55dea46ace8b26159ca82db19/20260826-task1-4-w3-stock-01/`。
- stock源码先从executable pathname推导`application_home_dir`并调用`SetDllDirectoryW`，之后才进入`pyi_launch_execute`并以`LOAD_WITH_ALTERED_SEARCH_PATH`加载Python DLL；Python interpreter、bootstrap与`pyimod03_ctypes`均在其后。Task 1.1 inventory/Analysis确认该baseline实际包含`_ctypes.pyd`及`ctypes`模块，因此后置ctypes fallback的`sys._MEIPASS + PATH`搜索适用于本baseline。所审源码没有W3所需的`SetDefaultDllDirectories`/`AddDllDirectory`组合、`GetFileInformationByHandleEx`/`FILE_ID_INFO` live identity、final-component no-follow、native attestation handoff或`TrustedSourceLoader`。
- stock `runw.exe`与baseline windowed EXE均为AMD64 GUI PE、无delay imports，静态导入`ADVAPI32.dll`、`COMCTL32.dll`、`GDI32.dll`、`KERNEL32.dll`、`USER32.dll`；静态名称不能证明实际KnownDLL/System32 identity，且`COMCTL32.dll`需要在W3中明确消除pre-entry依赖或批准并证明其WinSxS/System resolution closure。
- 六项mandatory stock断言`DLL_POLICY_BEFORE_PYTHON`、`NATIVE_CLOSURE_PRELOAD`、`ACTUAL_MODULE_REPROOF`、`ATTESTATION_HANDOFF`、`PREAUTHORITY_HOOK`、`PE_SYSTEM_ONLY`全部FAIL。`module_collection_mode='py'`只能证明collection机制可用，不能代答native pre-authority与exact executed-byte proof；Task 1.4据此完成stock route NO-GO裁决并阻断所有frozen consumer与Task 7，完整W3 feasibility转由Task 1.5/1.6负责。
- Task 1.4在未materialize native toolchain的分析profile中完成stock能力审计；Task 1.5另行materialize并锁定MSVC x64、Windows SDK、bootloader source及runtime输入，供custom entry reapproval与后续spike使用。
- `tools/replay_w3_stock_bootloader.ps1`现以显式CPython、Task 1.1 artifact key、空work/output roots为输入，创建clean audit venv、下载并校验固定PyInstaller sdist/stock bootloader、提取source后重跑关系审计；不再依赖本机sibling目录布局。Task 1.1 baseline artifact仍是显式content-addressed外部输入，而不是被重复提交到Git的322MB build/dist树。

### Implement Notes — Source / frozen delivery staging

- stock NO-GO只证伪未经修改的PyInstaller entry，不证伪Windows source业务能力。Task 2～6.6b先以用户安装的CPython 3.14 x64、专用venv、`requirements-ui.txt`与受审source tree闭合Qt、Project、TM、TMX与FTS5；6.6a只实现轻量入口，WA-08 5.4a使用它运行产品journey，6.6b再汇总source里程碑。该入口只调用绝对`pythonw.exe`/source bootstrap，不属于frozen release。
- W3没有降级：custom in-process entry的toolchain、ABI、Boot TCB与维护边界由Task 1.5立即规划；Task 1.6在W1 rooted contract冻结后执行gate-quality native spike。spike通过后只先放行pre-build roots/WA-07 3.6a与candidate构建；所有packaged owner revalidation必须等待platform 7.4同一发行候选，随后才由WA-08汇合。

### Implement Notes — Task 1 evidence environment profile V2（2026-08-27）

- Windows servicing把system Windows PowerShell从`5.1.26100.8875`更新到`5.1.26100.9168`；实际success命令exit 0，但V1逐Build/Revision expectation产生`EVIDENCE.SCENARIO.OBSERVATION_MISMATCH`。该差异不改变LocalCAT command、containment或W3 TCB语义。
- `CLEAN_WINDOWS_V1`及evidence/contract schema v1保持只读兼容，用于既有artifact复核；新scenario contracts采用schema v2 + `CLEAN_WINDOWS_V2`，仍从`GetWindowsDirectoryW`得到的system32绝对路径启动并硬门`PSEdition=Desktop`、5.1 major/minor、`RuntimeInformation.ProcessArchitecture=X64`。每条command在suspended process边界用`QueryFullProcessImageNameW`核对system32 selection并重算实际image SHA-256，再由同一进程输出identity prelude；极短timeout未输出prelude时，只有launch digest仍等于初始化probe digest才复用缓存identity。command record保存完整四段Build/Revision、edition、process architecture、system32 selection role和实际PowerShell binary SHA-256；只有servicing字段从阻断oracle降为审计事实，其他identity、环境projection和行为结果仍fail closed。旧schema v1 success bundle已用新validator走完整manifest/contract/checksum验证为PASS；producer则对v1合同尽早拒绝，避免生成代际不匹配bundle。

### Implement Notes — Task 1.5 custom entry planning（2026-08-27）

- Task 1.4的stock顺序事实使父wrapper和Python runtime hook都不能成为W3 entry。`w3-custom-entry-plan.md`据此选择同一进程、release-owned的PyInstaller 6.22.2 `runw`下游patch，并固定E0～E11时序、pre-link/runtime/post-build manifest分层、one-shot opaque extension authority、最小Boot TCB与攻击矩阵。
- PyInstaller官方支持用Visual C++从sdist重建Windows bootloader，且MSVC路线可生成self-contained static executable；本路线因此选择MSVC x64。Task 1.5以构建前materialized的compiler/SDK/tool与upstream/runtime摘要、patch owner/apply合同、expected native/system allowlist及exact C API目标表作为W3审批输入；Task 1.6在W1冻结后实现patch并生成绑定该输入digest的realized-build lock。
- `tools/prepare_windows_frozen_entry_inputs.py`与`packaging/windows/frozen-entry/candidate-contract.json`已生成`candidate-input.lock.json`：candidate-input digest为`06c37c4bf251700bdb16e5b4d288b3c8474e87d637ce7a290d9c33b1586e9619`，lock文件SHA-256为`703cfcdc194c49c9631a8d38c446b67c948113beaf21669fa5482b4cd8ebc2c5`。锁定的SDK输入闭包是构建实际可达的x64 `bin`、`include`与`lib`聚合。system Windows PowerShell 5.1 wrapper在两个不同空work root从pristine source建立精确MSVC/SDK环境、核对`cl/link/rc/mt`实际解析路径并以`-j1`现场重建stock probe；Waf运行前的实际build-copy source与完整bootloader build-driver aggregate形成pre-build evidence `3792488f8b51f6854702b8a5b10983fde4b1c3377086fac661096657d12a0dc6`，tool identity、arguments、净化environment、路径/耗时归一化stdout/stderr digest与result import digest形成canonical build evidence `57d4a3b089cfd7d25a9c60496a595d9afb6c3ca22f902125db34f858f0e32547`并进入lock；三个JSON producer显式输出UTF-8/LF，使仓库规范化checkout与Windows现场生成物具有同一字节身份；两次得到byte-identical evidence与lock，七项input assertion及25项自动测试PASS。producer Python source/native base runtime、venv语义/launcher、实际`pefile` metadata、`vswhere`与生成/重放工具本身也进入摘要。
- CPython 3.14目标表由PyInstaller PEP 741实际43-symbol路径和custom extension 19个function/1个data export组成，共63项且全部由锁定`python314.dll`导出；base binding header摘要、19个新增function签名与data export地址类型均进入exact合同，built-in注册统一使用`PyInitConfig_AddModule`。expected E0 import表从锁定stock `runw`逐symbol派生，删除`COMCTL32`和`SetDllDirectoryW`后只保留四个获批system DLL并加入rooted/identity/policy及所选MSVC代码生成所需Kernel32 API；realized import与native closure由1.6产物复核。
- 同一materialized MSVC/SDK已从官方sdist现场构建`run/run_d/runw/runw_d`；candidate lock只把该stock rebuild的AMD64、无delay-import、exact import-table与相对官方stock的五项compiler-generated symbol delta作为1.5选择事实，不把未使用`/Brepro`的probe PE bytes当发行authority。custom patch、固定编译/链接flag、双clean build byte identity与完整能力结论仍由1.6 mandatory matrix给出。

### Implement Notes — Task 3 Windows rooted authority（2026-08-28）

- `GetVolumeInformationByHandleW`可直接查询保留的目录与Volume GUID根目录handle；本机对去除尾随分隔符的Volume GUID设备路径不能直接建立同类目录authority。`FILE_ID_INFO`的64-bit volume identity与`GetVolumeInformationByHandleW`的DWORD serial保持分域，不截断比较；ADR-025采纳后，storage device/cache枚举不再属于普通publish host gate，`DURABILITY_UNAVAILABLE`只用于arm前local fixed NTFS/API/flush/write-through/handle-bound naming等documented capability缺失。
- Win32文档化接口没有等价`openat(parent_handle, name)`的逐组件入口；首版因此从drive root构造Volume GUID累计路径，对每个组件执行no-follow ENTRY_PROBE并与随后保留的directory handle比较live identity，完整保留root chain，采用exact final-path spelling策略。真实junction、symlink、intermediate swap、ancestor rename与share矩阵均在正文读取前稳定关闭逃逸；本阶段不需要转向未文档化native API。
- `CreateRestrictedToken`的默认restriction profile会同时拒绝读取；Task 3.3需要的同SID“可读但不可写/删”反例由`WRITE_RESTRICTED`精确表达，并以真实primary-token子进程验证。普通UAC limited/medium与同一TokenUser的elevated/high current-primary均完成实际读、写与删除正向控制；test-only native helper及其MSVC/SDK输入不进入runtime或发行依赖。
- 当前Windows 11对`GetTokenInformation(TokenHasRestrictions)`接受`DWORD`容量但可能返回`ReturnLength=1`，而公开说明以`DWORD`描述结果。测试因此只接受现场1-byte形态或完整4-byte形态，并以哨兵确认1-byte调用没有越过报告长度；其他token information class仍要求各自公开的精确尺寸。

### Implement Notes — Task 2.1 persistent private proof contract（2026-08-28）

- Task 3.4需要的跨重启proof不能由只表示当次ACL/MIC检查的`PrivateAccessEvidence`承载。共享合同因此保留既有physical proof不变，另定义不并入`PlatformFileBackend` aggregate的`PersistentPrivateProof`：raw exact 32-byte secret派生SHA-256 key id，MAC输入为固定domain tag加排除MAC字段的canonical unsigned projection；device-secret binding独立retain source handle，同issuer exact authority经过terminal reproof后只能消费一次。真实持久文件读取前还须固定codec输入上限，生产secret authority须采用无公开状态的封闭具体类型；这两项由Task 3.4实现与复审闭合。

### Implement Notes — Task 2.4 Windows persistent proof narrowing（2026-08-28）

- `narrow_windows_persistent_private_proof(backend, probe_root)`只在Windows导入native adapter，要求传入对象是模块声明的exact `WindowsPlatformAdapter`且同时满足aggregate与独立persistent port；它对显式absolute root重新执行live read-only self-probe并返回同一对象。foreign/subclass/缺失shape或probe fault均归一为稳定capability failure，POSIX/unknown host不加载Windows模块。

### Implement Notes — Task 0.6 ADR-024/025 activity sync（2026-08-28）

- ADR-024确认LocalCAT不消费identity-provider来源：blocking evidence只按process primary token的canonical `TokenUser` SID、token type、integrity、AppContainer/session与实际ACL/MIC access facts判断。standard/elevated、同SID low-integrity/restricted、thread impersonation及service/AppContainer/impersonation边界仍是mandatory；domain/Entra仅为可用时的非阻断兼容性覆盖。
- ADR-025确认普通Windows发布资格是`WindowsDocumentedPublishV1`，与既有POSIX fsync/directory-fsync产品合同同层：local fixed NTFS、`WRITE_THROUGH`+`FlushFileBuffers`、handle-bound naming、candidate close、retained readback、owner commit与terminal reproof。arm前能力缺失零命名mutation并返回`DURABILITY_UNAVAILABLE`；arm后不确定返回`RECOVERY_REQUIRED`。process termination、instruction fault、应用重启与正常OS reboot是blocking恢复证据；forced-power-off与硬件cache/power-protection资格只有未来独立产品声明才可重新引入。
- 该分层只从frozen闭包删除durability registry输入；ADR-022的native entry、Boot TCB、DLL/source/fixture authority、clean build provenance与真实packaged E2E没有降级。

## Codebase Gap Analysis

### 现有边界与可复用资产
- `parser_source.py:97-115` 已集中定义 rooted capability fail-closed 入口，公共 `SourceReference`、sealed snapshot 和稳定错误映射可保留。
- `project_package.py`、`tm_snapshot_artifacts.py` 等已经采用“绑定 parent、校验 identity、candidate、replace、fsync、LKG/recovery”的协议形状；业务状态机和错误码应复用，而不是重写。
- `tm_migration.py:4895-5214` 已定义 persistent per-resource reservation 的 payload、单链接约束、reprove、崩溃释放意图；只需把具体 POSIX lock/identity 操作下沉到共享能力。
- `capability_host.py` 已把 `__file__`、`ModuleSpec`、loader path、代码对象和fixture digest绑定成fail-closed图；Windows frozen amendment必须在不弱化Gate的前提下把metadata proof升级为retained-handle source digest + loader attestation + exact-byte compile proof。
- `requirements-ui.txt`、`tm.jsonl`、`terms.csv`、`LocalCAT-logo-silver.png`、`benchmark_tm_contract.json` 和 Gate A/C fixtures 均在源码树中；Windows `.ico` 和受版本控制 PyInstaller spec 当前不存在。Qt avatar catalog 的 Windows 功能探针已确认匹配头像能够索引、解码并按 UI 尺寸渲染。

### 需要迁移的代码簇

| 簇 | 代表文件 | 当前平台耦合 | 目标边界 |
|---|---|---|---|
| Rooted source/writer | `parser_source.py` | `os.name == posix`、`dir_fd`、`O_NOFOLLOW`、`pread` | `RootedFileSystem` port + backend-neutral sealed I/O |
| Chunk durable store | `collaborative_chunk_store.py` | 顶层 `fcntl`、`flock`、dirfd replace/fsync | `ProcessFileLock` + `BoundDirectoryPublisher` |
| 项目 persistence | `project_package.py`, `project_workspace_intake.py` | `st_dev/st_ino`、dirfd replace/unlink/fsync | 共享 identity/publish/recovery port |
| Resource/TMX persistence | `resource_artifact_save.py`, `resource_package.py`, `tmx_artifact_save.py` | 重复 parent binding 与 POSIX publish | 同一共享平台边界，保留业务 receipts |
| TM activation/snapshot | `tm_migration.py`, `tm_content_attestation.py`, `tm_snapshot_artifacts.py`, `tm_snapshot_recovery.py`, `tm_activation_*` | `flock`、mode/uid/dev/ino/nlink、dirfd durability | lock + platform identity + private storage proof |
| TM secondary writers/tools | `tm_stage_sealer.py`, `tm_schema_upgrade.py`, `tm_sqlite_store.py`, benchmark/process tools | `fsync`/identity/平台假设 | 统一 capability 或明确仅 file-handle flush |
| Frozen composition | `capability_host.py`, `qt_editor.py` | 假定 checkout 中真实源码/fixture | bundle root + generated frozen closure + external source loader |
| Tests | Parser/TM/Qt/Project tests | 多处 `skipUnless(os.name == "posix")` | 共享合同测试 + Windows 反例 + packaged E2E |

## Research Log

### Windows rooted handle、reparse 与 identity
- **Context**: POSIX `openat(..., O_NOFOLLOW, dir_fd=...)` 在 CPython Windows 上不可用；必须证明逐组件 containment，而不是调用 `Path.resolve()`。
- **Sources Consulted**:
  - [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)
  - [GetFinalPathNameByHandleW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew)
  - [GetFileInformationByHandleEx](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex)
  - [FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)
  - [Reparse Point Operations](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-point-operations)
  - [Hard Links and Junctions](https://learn.microsoft.com/en-us/windows/win32/fileio/hard-links-and-junctions)
- **Findings**:
  - 目录 handle 需要 `FILE_FLAG_BACKUP_SEMANTICS`；`FILE_FLAG_OPEN_REPARSE_POINT` 令最终组件不跟随 reparse。
  - `FILE_SHARE_DELETE` 同时允许 delete 与 rename；share options 在 handle 关闭前一直生效。因此 root/intermediate directory handle 只共享读取，不共享 write/delete，可拒绝会改写或重命名该目录对象的新 handle；已固定祖先让下一次 cumulative open 不会穿过后来替换的 ancestor。
  - `GetFinalPathNameByHandleW` 给出 fully-resolved final path，但路径字符串只能作为佐证；`FILE_ID_INFO` 的 volume serial + 128-bit file ID 只用于比较两个 live handles 是否同一对象。Microsoft 不保证 FileId 随时间永久唯一，文件删除后 identity 可复用。
  - junction 是 reparse point；hard link 不是。安全关键单链接文件仍需 link-count=1，普通只读 source 可接受 hardlink 但其 sealed content/identity 必须稳定。
- **Implications**:
  - Windows backend 需要直接调用 Win32 handle API；`pathlib`/`os.stat` 不能作为 authority。
  - 支持范围需按 volume/file-system capability 探测；无法提供 FileId、reparse 和 ACL proof 的 volume 应 fail closed。跨重启 authority 必须重开并验证 content/phase/private/device-secret proof，不能只比历史 FileId。
  - 逐组件 handle walk 必须明确每种 handle 的 share mask 和 lifetime。

### Windows handle-relative rename、replace 与 durability
- **Context**: 当前代码大量使用 `os.replace(..., src_dir_fd=..., dst_dir_fd=...)` 与目录 `fsync`；Windows 普通 Python 路径 API不能表达等价 authority。
- **Sources Consulted**:
  - [SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle)
  - [FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info)
  - [NtSetInformationFile](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile)
  - [MS-FSA FileRenameInformation](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/87f86c9b-6c2a-4803-84b7-131a74a434fa)
  - [ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)
  - [FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers)
- **Findings**:
  - 实机Win11反例表明，`SetFileInformationByHandle(FileRenameInfo[Ex])`无法在retained parent `share=READ`下完成同目录handle-bound命名：完整路径返回sharing violation，`RootDirectory=parent`+basename返回invalid parameter，`RootDirectory=NULL`+basename则按CWD解释。
  - `NtSetInformationFile(FileRenameInformation)`在`RootDirectory=NULL`且名称为不含separator的单一组件时，按MS-FSA直接使用candidate open的`Open.Link.ParentFile`；实机在parent仍为`share=READ`时成功，rename后candidate handle identity与readback继续有效。该路径不需要扩大root/ancestor share，也不经过CWD或重新打开source pathname。
  - `ReplaceFileW` 能替换并保留一部分属性，但其 replacement 打开方式无 share mode，且失败码可能留下多种命名状态；它适合作为比较/回退研究，不应在未建 recovery 分类时直接替换 POSIX `os.replace`。
  - `FlushFileBuffers` 只明确保证指定 file handle 的 buffered data 被写出。`FILE_FLAG_WRITE_THROUGH` 文档说明 NTFS 会及时 flush 由操作引起的 metadata（包括 rename），但 `REPLACEFILE_WRITE_THROUGH` 明确“不支持”。
  - candidate 以 share=none 打开时，rename 后仍持有该 handle 会阻止同进程 reopen destination；正确生命周期必须是 rename、capture final facts、close candidate、再 reopen/readback。
  - commit 前关闭的 target snapshot 不是 expected-target 原子 CAS；不合作进程可在 snapshot 与 rename 之间替换目标。平台层只能提供 create-if-absent 或 owner 持排他资源锁时的 replace facts。
- **Implications**:
  - 同父目录发布采用`CREATE_NEW` candidate handle + file flush + `NtSetInformationFile(FileRenameInformation, RootDirectory=NULL, FileName=validated basename)`；调用前重验candidate的live parent chain与bound parent完全相同。固定结构布局/精确长度/information class/NTSTATUS映射并做arm前self-probe；不可用时零命名mutation返回`DURABILITY_UNAVAILABLE`，禁止CWD、完整路径、Win32 wrapper或跨父目录fallback。
  - ADR-025已经定义普通产品的命名success boundary：在local fixed NTFS上以`WRITE_THROUGH`、`FlushFileBuffers`、handle-bound naming、candidate close、retained readback、owner commit与terminal reproof闭合`WindowsDocumentedPublishV1`。process termination、instruction fault、应用重启与正常OS reboot验证协议恢复；它们不宣称forced-power-loss硬件认证，后者若成为产品需求须另立Spec/ADR。

### Windows 跨进程锁
- **Context**: `collaborative_chunk_store` 和 TM activation 使用 `flock`，Windows 无 `fcntl`。
- **Sources Consulted**:
  - [LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)
  - [Python `msvcrt.locking`](https://docs.python.org/3/library/msvcrt.html)
- **Findings**:
  - `LockFileEx` 可请求 exclusive/shared、阻塞/立即失败；进程终止或 handle 关闭后由操作系统释放，但释放可能不是瞬时，因此测试不得承诺公平或零延迟。
  - `msvcrt.locking` 只暴露 CRT byte-range 锁，阻塞模式固定重试十次、每次一秒；不利于精确映射稳定 timeout/competition 结果。
- **Implications**:
  - 共享 Windows backend 直接封装 `LockFileEx/UnlockFileEx`，并将 handle identity、byte range、等待策略和错误映射纳入合同。
  - 锁文件持久存在且不unlink；W1自行拥有protocol-control integrity ACL、payload和single-link proof，W2只拥有attestation/device-secret private representation，从而避免`W1 lock → W2 private proof → W1 rooted/publish`循环。
  - 首次creator必须以share-none init handle写入可重算versioned payload并flush/readback；并发loser重试。creator crash后只允许在exact ACL/root/single-link复证与exclusive init open下恢复空/expected strict-prefix/完整expected bytes，未知payload永不自动unlink/replace。

### Windows private storage、owner 与 attestation identity
- **Context**: ADR-013 当前以 `0700` directory、`0600` files、uid、mode、nlink、dev/inode 和 directory fsync 表达 device-local attestation；这些值不能原样搬到 Windows。
- **Sources Consulted**:
  - [Python `os.chmod` / Windows `mkdir(0o700)`](https://docs.python.org/3/library/os.html)
  - [File Security and Access Rights](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights)
  - [GetSecurityInfo](https://learn.microsoft.com/en-us/windows/win32/api/aclapi/nf-aclapi-getsecurityinfo)
  - [SECURITY_ATTRIBUTES](https://learn.microsoft.com/en-us/windows/win32/api/wtypesbase/ns-wtypesbase-security_attributes)
  - [AccessCheck](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-accesscheck)
- **Findings**:
  - Windows `chmod` 只设置 read-only；不能证明 POSIX mode。CPython 3.14 的 `mkdir(mode=0o700)` 会建立仅当前用户和 administrators 可访问的目录，但安全关键文件仍需显式创建/验证 security descriptor。
  - Windows owner 与权限由 SID/security descriptor/DACL 表示；可按 handle 用 `GetSecurityInfo` 验证。
  - default DACL 来自 access token/parent，不能在不同配置上默认为满足“device-local private”。
- **Implications**:
  - ADR-013/016 的 Windows 表示由ADR-021/023/024固定：process-primary `TokenUser`/owner SID、精确ACE rights/order、AccessCheck/generic mapping、inheritance/protected状态、nlink、live volume/file ID、reparse-free及重启后exact digest/phase/private/device-secret重新证明。
  - 标准/UAC elevated进程按canonical SID与token shape判断，不能靠账户名或provider推断等价性；同SID low-integrity/restricted、thread impersonation和service/AppContainer/impersonation边界必须fail closed。domain/Entra环境只作optional兼容性覆盖，不进入blocking矩阵。

### Frozen-source 与 PyInstaller
- **Context**: 基线 onedir/windowed 没有真实 `.py`、fixtures 或数据；`capability_host` 因 `capability_host.py` 不存在而 fail closed。
- **Sources Consulted**:
  - [PyInstaller Spec Files](https://pyinstaller.org/en/latest/spec-files.html)
  - [PyInstaller Hook `module_collection_mode`](https://pyinstaller.org/en/latest/hooks.html)
  - [PyInstaller Runtime Information](https://pyinstaller.org/en/stable/runtime-information.html)
- **Findings**:
  - spec 的 `datas` 是显式数据收集入口；普通 pure modules 默认进入 PYZ。
  - hook 的`module_collection_mode='py'`会把指定模块作为外部源`.py`收集且不收集对应PyInstaller bytecode；`pyz+py`会造成运行来源与外置源不一致。
  - bundled `__file__`/`spec.origin`/`co_filename`即使一致也不证明实际执行bytes来自该`.py`：CPython `SourceLoader`可消费bytecode cache，filename仍报告source path。critical source必须拒绝`.pyc`/`__pycache__`/PYZ duplicate，并由trusted loader从retained verified handle读取exact bytes后直接编译。
  - PyInstaller bootloader在Python-level项目bootstrap前已加载Python DLL、设置runtime环境并执行runtime hooks；因此DLL policy必须由release-owned native entry在首次Python DLL/非KnownDLL load前建立，Python bootstrap不能追溯保护这些load。完整TCB还包含必要stdlib/ctypes/hash/manifest/loader和native DLL，必须区分“唯一项目级bootstrap authority”与“完整Boot TCB”。
- **Implications**:
  - 从Gate roots和frozen owner manifest生成module/data/Boot-TCB closure；production build只接受clean tracked tree，并对spec/hooks/generator/bootstrap/roots、locked wheels、release-owned native bootloader/Python/native runtime逐项内容寻址；审计entry前PE imports/delay-load并保存native DLL load attestation。
  - 构建后probe逐个断言`.py` retained-handle digest、`TrustedSourceLoader` direct-compile attestation、origin/co_filename、fixture digest、DLL inventory、资源路径和repository-path absence。
  - W3批准后的第一个实现证据必须是最小frozen spike，验证exact executed-source bytes、无`.pyc`/`__pycache__`/PYZ duplicate、bootstrap handle-read、DLL path injection和reparse/swap fail-closed；失败先重开W3。

### Qt 与 SQLite 发行运行时
- **Context**: venv 已通过，但 frozen 产物必须在无仓库/无 Qt 环境独立验证。
- **Sources Consulted**:
  - [Qt for Windows Deployment](https://doc.qt.io/qt-6/windows-deployment.html)
  - [SQLite FTS5](https://www.sqlite.org/fts5.html)
  - [SQLite compile option `SQLITE_ENABLE_FTS5`](https://sqlite.org/compile.html)
- **Findings**:
  - Qt GUI 在 Windows 必须有 `platforms/qwindows.dll`；其他实际使用的 image/icon/style plugins 也需保持正确目录。
  - FTS5 的 trigram tokenizer 是实际建表/query 能力，不能只检查 compile option 字符串。
- **Implications**:
  - 发行验收必须创建真实可见 Qt 窗口，并真实建表、写入、MATCH 查询、关闭、重开查询。

## Architecture Pattern Evaluation

### 平台边界

| Option | Description | Strengths | Risks / Limitations | Disposition |
|---|---|---|---|---|
| A. 每个模块加 `sys.platform` 分支 | 就地替换 `fcntl`/dirfd | 初始改动小 | 43 文件重复安全逻辑、错误漂移、不可统一反例 | Rejected |
| B. 共享 ports + POSIX/Windows adapters | 业务层依赖 rooted/lock/publish/private-proof 合同 | 单一证明面、可运行共享合同测试、保留业务状态机 | 初期需要清晰拆层，属于治理边界变更 | **Proposed** |
| C. 引入 `pywin32` | 使用封装后的 Win32 API | API 覆盖丰富 | 新 runtime/frozen 依赖、对象封装与错误转换需额外证明 | Backup only |
| D. `pathlib.resolve` + `os.replace` | 用普通路径近似 | 简单 | TOCTOU、reparse、share/durability 无证明 | Prohibited |

### Rooted traversal / publish

| Option | Description | Strengths | Risks / Limitations | Disposition |
|---|---|---|---|---|
| Win32 documented handle walk | `CreateFileW` 逐组件、ancestor 无 delete-share、reparse/FileId/final-path proof | 无新增依赖；全部为文档化桌面 API | share/lifetime 必须精确；仅在可证明 volume 启用 | **Proposed** |
| `OpenFileById` 主路径 | 目录枚举 ID 后按 ID 打开 | identity 强 | SMB 不支持；创建新 entry 不方便 | Counterexample/helper only |
| `NtCreateFile` RootDirectory | Native API handle-relative open/create | 最接近 `openat` | user-mode 支持/ABI 治理和维护成本高 | Escalation fallback if documented APIs fail tests |
| 字符串 canonicalization | `resolve`/prefix compare | 无 FFI | 不抗竞态/reparse | Rejected |

### Frozen-source

| Option | Description | Strengths | Risks / Limitations | Disposition |
|---|---|---|---|---|
| external source + retained-handle trusted loader | `module_collection_mode='py'`收集真实`.py`，bootstrap loader从已验证handle读取并直接编译 | 绑定被验证与被执行bytes | 需W3冻结完整TCB、loader attestation和bytecode拒绝 | **Proposed** |
| `pyz+py` 双份 | PYZ 执行 + data 中另有 `.py` | 便于查阅 | 运行来源可能不是被验证文件 | Rejected unless proof shows exact binding |
| 新 frozen manifest 信任 | 改为摘要/signature manifest | 可减少裸源码 | 改变现有 source identity 与信任根 | Deferred; requires separate ADR/counterexamples |
| 禁用 Gate | frozen 直接可启动 | 无 | 明确违反能力合同 | Prohibited |

## Proposed Design Decisions（均以治理批准为前提）

### Decision 1：建立单一平台文件能力边界
- **Selected Approach**: 新建纯合同模块和 POSIX/Windows adapters；业务模块只接收 `RootedFileSystem`、`ProcessFileLock`、`AtomicPublisher`、`PrivateStorageProof`。
- **Rationale**: 当前重复实现已跨 Parser、项目、资源、TM、协作分工；单一边界才能让同一反例矩阵约束全部消费者。
- **Trade-offs**: 首个簇改动较大，但后续业务接入变为受控迁移。
- **Follow-up**: 形成 ADR 候选并在实现前由人类审批所有权、稳定错误族和支持 volume。

### Decision 2：Windows 使用文档化 handle walk + FileId proof
- **Selected Approach**: 逐组件 `CreateFileW`；目录使用 `BACKUP_SEMANTICS|OPEN_REPARSE_POINT`，拒绝 reparse，保留只共享读取且不共享 write/delete 的 ancestor handles；capture/reprove normalized final path 与 `FILE_ID_INFO`。
- **Rationale**: 不依赖 CWD/字符串前缀，并利用 Windows share semantics 固定 ancestor。
- **Trade-offs**: Windows local volume 是首个受支持目标；remote/不提供完整 proof 的文件系统 fail closed。
- **Follow-up**: junction、symlink、mount-point、ancestor swap、hardlink、FileId reuse/restart 反例必须先通过；失败则评估 `NtCreateFile` fallback 并重新审 ADR。

### Decision 3：锁与发布保持 handle-bound
- **Selected Approach**: `LockFileEx`保护W1 integrity-ACL persistent single-link lock file；candidate用`CREATE_NEW`与请求的security profile创建，rename后关闭candidate再reopen/readback，并保留no-write/no-delete destination handle到owner durable commit与terminal reproof；replace只在owner-defined排他资源lock下允许。
- **Rationale**: 崩溃释放、跨进程互斥、source identity 与 destination parent 都能绑定 handle。
- **Trade-offs**: 平台层不提供 expected-target CAS；不合作同用户进程仍可能竞态。需要更强保护的 owner 使用 immutable generation + journal/pointer。`WindowsDocumentedPublishV1`只声明OS文档化flush/naming与业务恢复闭合，不提供突然断电硬件认证；不承诺 lock fairness。

### Decision 4：由 W3 建立 frozen-source/bootstrap 合同
- **Selected Approach**: PyInstaller `--onedir --windowed` + release-owned native bootloader/generated spec/hook；native entry在首次Python DLL/非KnownDLL load前固定搜索、绑定bundle并移交attestation，关键模块`module_collection_mode='py'`且无bytecode/PYZ duplicate；Python bootstrap闭合其余Boot TCB/source/fixture handles，`TrustedSourceLoader`从retained handle读取、摘要、直接编译exact bytes，再铸造`TrustedSourceAuthority`。W3治理可与W1并行，bootstrap实现必须满足已采纳W1 invariants。
- **Rationale**: ADR-011 只拥有 Feature 5/UI DTO 与 composition，不是 source-loader trust authority；W3 必须明确建立 loader、bootstrap、manifest 和长期 build ownership 合同，不能把现状误称为 ADR-011 已批准。
- **Trade-offs**: 发行物包含可读源码且体积增加；这是当前能力合同的显式成本。

### Decision 5：按source/frozen双路线分簇交付
1. Governance/ADR/ownership closure与stock entry NO-GO裁决。
2. source主线：平台合同、Windows native primitives、POSIX parity与反例harness → Parser/Chunk startup → Project/Resource/TMX → TM/Feature5/UI → user-managed source journey。
3. frozen并行规划：立即冻结custom in-process entry的toolchain、ABI、Boot TCB与维护边界；W1 rooted contract冻结后执行最小TrustedSourceLoader exact-byte spike，失败回到W3但不撤销source里程碑。
4. spike全PASS后进入frozen owner revalidation、完整source/build manifest、Qt resources与WA-07/08 packaged journey。
5. clean Windows packaged E2E + macOS/Linux regression + release evidence。

## Governance Candidates

依据 `.kiro/settings/rules/governance.md:13-23`，以下不是普通本地 bug fix：

1. **ADR candidate W1 — Cross-platform rooted file authority, lock and durable publish boundary**
   - 改变共享 dependency/layer/composition boundary。
   - 定义Windows live-handle identity、reparse/share mode、W1 lock integrity ACL、retained readback→owner commit→terminal reproof、非CAS threat scope与稳定错误族；其后来加入的`DurabilityProfile`/硬断电门已由ADR-025取代为`WindowsDocumentedPublishV1`。
   - 与 ADR-008/009/018/019 的 authority/atomicity/fail-closed 决策相交。
2. **ADR candidate W2 — Windows device-local private attestation representation**
   - ADR-021最初在Windows物理表示范围部分取代ADR-013/016的POSIX uid/mode/dev/inode谓词并冻结`WindowsPrivateAclV1`；Task 0.5随后形成并获采纳的ADR-023，以包含exact owner+DACL+MIC projection的`WindowsPrivateSecurityV2`接管该物理profile。nested `WindowsPrivateProof`与Gate D/canonical owner envelopes仍保持正交。
   - 改变 frozen/persistent identity 与安全恢复边界。
3. **ADR candidate W3 — Windows frozen distribution ownership and source closure**
   - 新建onedir/windowed build owner、native-entry pre-Python DLL policy、完整Boot TCB、retained-handle exact-byte `TrustedSourceLoader`、`TrustedSourceAuthority`、clean/content-addressed source/fixture closure及发行门。
   - ADR-011 仍只拥有 Feature 5/UI DTO 与 composition；W3 是新的跨 Spec frozen/source-loader contract 与长期所有权，满足候选门槛。

W1/W2/W3 已分别作为 ADR-020/021/022 正式采纳，ADR-023也已作为pre-authority时序补充与V2 security profile窄范围修订正式采纳；ADR-024进一步取代provider环境mandatory矩阵，ADR-025取代runtime durability registry与硬断电硬件资格门。取代/补充关系由治理分支唯一记录。`windows-platform-enablement` owning scope、`ui-mvp` 的 TMX owner 身份和 avatar 仅回归边界已由 Task 0.2 批准，WA-01～08 current R/D/T acknowledgement、ledger authority和独立Design approval均已闭合；实现按依赖逐簇进入，仍不得用局部能力代答最终EXE。

## Risks & Mitigations
- **Windows share mask 自阻塞** — 为 root/intermediate/source/target/candidate/lock 定义不同 handle profiles；运行占用目标、同进程二次打开和跨进程 replace 矩阵。
- **路径/publish proof 只在受支持NTFS成立** — 探测local fixed volume、FileId/reparse/ACL与documented flush/write-through/naming capabilities；首版只在完整前置通过的NTFS启用，其余fail closed。
- **native same-parent naming failure residue** — candidate identity、journal phase、readback 和 LKG recovery 共同分类；固定`NtSetInformationFile` ABI/NTSTATUS映射并对每个 API failure point注入测试，不回退到路径式rename。
- **pre-snapshot 被误当 CAS** — 仅允许 create-if-absent 或 resource lease 下 replace；记录不合作 writer threat scope，加入 instruction-boundary swap；需要强 CAS 的 owner 使用 immutable generation+journal/pointer。
- **FileId reuse 被误当永久身份** — FileId 只比较 live handles；receipt 绑定 digest/phase/private/device-secret并在重启后重新证明，覆盖 delete/recreate reuse。
- **协议恢复被误当硬件掉电认证** — `WindowsDocumentedPublishV1`以instruction fault、process termination、应用重启与正常OS reboot验证old/new/recovery-only；明确不宣称forced-power-loss存活，未来硬件认证必须独立治理。
- **Windows ACL 与 POSIX mode 不等价** — ADR 固定 SID/DACL 规则，按 handle 重验 owner、DACL、inheritance、nlink；不使用 `chmod(0600)` 假通过。
- **W2 proof 形成 self-MAC 或吞并业务 envelope** — business owner先对不含proof的canonical private-context取摘要，W2只对domain-separated unsigned proof projection做HMAC；Gate D/canonical envelope仍由原owner解释和发布。
- **Python bootstrap被误写成最早DLL authority** — native bootloader已先加载Python DLL；W3必须在native entry/首次非KnownDLL load前固定搜索、绑定bundle并以受限绝对路径加载，Python层只消费handoff attestation。
- **同名source或bytecode抢先执行** — 关键模块source-only collection且拒绝`.pyc`/`__pycache__`/PYZ duplicate；bootstrap loader只编译retained verified handle的exact bytes，CapabilityHost核对loader attestation与source digest。
- **手写 manifest 漂移** — 从 Gate roots 与 owner manifest 生成闭包并在 CI diff/check；任何未声明动态 import 使 build gate 失败。
- **GUI windowed 无 stderr** — 提供受合同约束的 marker/log/exit probe；发布 E2E 同时运行真实 visible window 与 failure diagnostics。
- **范围过大导致半完成发布** — 每簇有 observable matrix，但只有最后 packaged clean-user matrix 全绿才将状态从 NOT_VERIFIED 改为 VERIFIED。

## References
- [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) — directory/reparse/share/write-through semantics。
- [Microsoft FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info) — Windows handle identity。
- [Microsoft GetFinalPathNameByHandleW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew) — final resolved path evidence。
- [Microsoft LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex) — cross-process lock/crash release。
- [Microsoft NtSetInformationFile](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/nf-ntifs-ntsetinformationfile) 与 [MS-FSA FileRenameInformation](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-fsa/87f86c9b-6c2a-4803-84b7-131a74a434fa) — candidate live parent上的同目录native rename语义；[SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle) 与 [FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info) 仅作为已证伪的Win32 wrapper对照。
- [Microsoft FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers) — file buffer flush boundary。
- [Microsoft File Security](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights) — owner/DACL model。
- [Python 3.14 `os`](https://docs.python.org/3/library/os.html) — platform availability and Windows permission limitations。
- [Python 3.14 `ctypes`](https://docs.python.org/3/library/ctypes.html) — `WinDLL(..., use_last_error=True)` FFI/error handling。
- [PyInstaller spec files](https://pyinstaller.org/en/latest/spec-files.html)、[hooks](https://pyinstaller.org/en/latest/hooks.html)、[runtime information](https://pyinstaller.org/en/stable/runtime-information.html) — source/data collection and bundle paths。
- [PyInstaller Building the Bootloader](https://pyinstaller.org/en/stable/bootloader-building.html) — 从sdist用MSVC重建Windows `runw`和static bootloader路线。
- [Visual Studio 2022 release history](https://learn.microsoft.com/en-us/visualstudio/releases/2022/release-history) — 受支持MSVC servicing候选与bootstrapper来源；materialized layout/catalog和toolchain lock提供candidate identity。
- [Microsoft Dynamic-link library search order](https://learn.microsoft.com/en-us/windows/win32/dlls/dynamic-link-library-search-order)、[`SetDefaultDllDirectories`](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-setdefaultdlldirectories)与[`LoadLibraryExW`](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryexw) — process policy及受限DLL search flags。
- [Python 3.14 interpreter initialization](https://docs.python.org/3.14/c-api/interp-lifecycle.html) — embedded interpreter必须在其他Python C API前初始化及`PyConfig`边界。
- [Windows PowerShell与PowerShell区别](https://learn.microsoft.com/en-us/powershell/scripting/what-is-windows-powershell) — Windows PowerShell 5.1是随Windows维护的系统组件；Build/Revision作为环境事实记录。
- [Qt Windows deployment](https://doc.qt.io/qt-6/windows-deployment.html) — `qwindows.dll` plugin layout。
- [SQLite FTS5](https://www.sqlite.org/fts5.html) — real FTS5/trigram behavior。
- Internal discovery artifacts are non-authoritative inputs；正式 evidence 必须按 Design 的仓库相对 artifact key、manifest 与 checksum 生成，不能以本机绝对路径充当身份。
