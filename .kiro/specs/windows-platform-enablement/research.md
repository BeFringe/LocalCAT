# Research & Design Decisions

## Summary
- **Feature**: `windows-platform-enablement`
- **Discovery Scope**: Complex Integration / Brownfield Platform Port / Frozen Distribution
- **Baseline**: `ui-mvp@b925b803d81001f55dea46ace8b26159ca82db19`，Windows 11、CPython 3.14.7 x64；本节矩阵是可移植的 spec-local baseline 摘要，原始现场报告只作 discovery 输入，不以本机绝对路径作为发行 evidence identity。
- **Key Findings**:
  - UI 依赖、PySide6/Qt、`qwindows.dll`、真实 Qt 窗口和 SQLite FTS5/trigram 均已通过；当前阻塞不是依赖安装或 Qt 插件。
  - 源码启动被 `collaborative_chunk_store.py` 顶层 `import fcntl` 阻断；Parser 之后又会按合同返回 `PARSER.SOURCE.ROOT_BINDING_UNAVAILABLE`。静态扫描命中 43 个源码/测试文件，说明这不是单文件兼容补丁。
  - Windows 的 share mode 本身是协议一部分：缺少 `FILE_SHARE_DELETE` 会阻止 rename/delete；这既能固定 rooted ancestor，也会在错误的 handle lifetime 下阻塞合法原子发布。
  - Windows 的 `VolumeSerialNumber + 128-bit FileId` 只适合比较同时存活的 handles；FileId 删除后可能复用，跨重启 receipt 必须另绑定 exact digest、phase/generation、private proof 与 device secret。reparse、ACL/SID、write-through 和 naming durability 的等价合同会改变跨 Spec 身份/持久化表示，满足 ADR 候选门槛。
  - PyInstaller 官方支持把指定模块以真实 `.py` 形式外置收集；这使保留现有 `SourceFileLoader` 身份合同成为首选，而不是发明弱化的 frozen boolean 或仅复制 data。

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

## Codebase Gap Analysis

### 现有边界与可复用资产
- `parser_source.py:97-115` 已集中定义 rooted capability fail-closed 入口，公共 `SourceReference`、sealed snapshot 和稳定错误映射可保留。
- `project_package.py`、`tm_snapshot_artifacts.py` 等已经采用“绑定 parent、校验 identity、candidate、replace、fsync、LKG/recovery”的协议形状；业务状态机和错误码应复用，而不是重写。
- `tm_migration.py:4895-5214` 已定义 persistent per-resource reservation 的 payload、单链接约束、reprove、崩溃释放意图；只需把具体 POSIX lock/identity 操作下沉到共享能力。
- `capability_host.py` 已把 `__file__`、`ModuleSpec`、`SourceFileLoader.path`、代码对象和 fixture digest 绑定成 fail-closed 图；优先满足现有合同。
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
  - [ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)
  - [FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers)
- **Findings**:
  - `FILE_RENAME_INFO.RootDirectory` 允许目标相对一个 directory handle 解析；源是已打开 candidate handle，因此比再次按源 pathname 打开更适合身份绑定发布。
  - `ReplaceFileW` 能替换并保留一部分属性，但其 replacement 打开方式无 share mode，且失败码可能留下多种命名状态；它适合作为比较/回退研究，不应在未建 recovery 分类时直接替换 POSIX `os.replace`。
  - `FlushFileBuffers` 只明确保证指定 file handle 的 buffered data 被写出。`FILE_FLAG_WRITE_THROUGH` 文档说明 NTFS 会及时 flush 由操作引起的 metadata（包括 rename），但 `REPLACEFILE_WRITE_THROUGH` 明确“不支持”。
  - candidate 以 share=none 打开时，rename 后仍持有该 handle 会阻止同进程 reopen destination；正确生命周期必须是 rename、capture final facts、close candidate、再 reopen/readback。
  - commit 前关闭的 target snapshot 不是 expected-target 原子 CAS；不合作进程可在 snapshot 与 rename 之间替换目标。平台层只能提供 create-if-absent 或 owner 持排他资源锁时的 replace facts。
- **Implications**:
  - 建议以 `CREATE_NEW` candidate handle + file flush + `SetFileInformationByHandle(FileRenameInfo[Ex], RootDirectory=bound parent)` 作为首选原子发布探针。
  - 命名 durability 不是可以在设计中默认成立的事实；必须通过新 ADR 定义受支持 volume、success boundary，并用真实 forced-power-off/reboot 验证 old/new/recovery-only。仅 process kill/fault injection 不足以宣称 durability。

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
  - 锁文件持久存在且不 unlink；创建、private ACL、payload 和 single-link proof 与 lock ownership 分离。

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
  - ADR-013/016 的 Windows 表示需新 ADR：primary-token `TokenUser`/owner SID、精确 ACE rights/order、AccessCheck/generic mapping、inheritance/protected 状态、nlink、live volume/file ID、reparse-free 及重启后 exact digest/phase/private/device-secret 重新证明。
  - 标准/UAC 提权、本地/domain/AzureAD 用户不能靠账户名推断等价性；必须按 SID 固定允许矩阵，service/AppContainer/impersonation token 首版 fail closed。

### Frozen-source 与 PyInstaller
- **Context**: 基线 onedir/windowed 没有真实 `.py`、fixtures 或数据；`capability_host` 因 `capability_host.py` 不存在而 fail closed。
- **Sources Consulted**:
  - [PyInstaller Spec Files](https://pyinstaller.org/en/latest/spec-files.html)
  - [PyInstaller Hook `module_collection_mode`](https://pyinstaller.org/en/latest/hooks.html)
  - [PyInstaller Runtime Information](https://pyinstaller.org/en/stable/runtime-information.html)
- **Findings**:
  - spec 的 `datas` 是显式数据收集入口；普通 pure modules 默认进入 PYZ。
  - hook 的 `module_collection_mode='py'` 会把指定模块作为外部源 `.py` 收集且不收集 bytecode；`pyz+py` 则可能造成运行来源与外置源不一致。
  - bundled `__file__` 会指向 bundle 内路径，但这不自动保证 loader、spec.origin、digest 和递归 fixture 闭包；现有 capability host 仍须实际 revalidate。
  - 当前 capability host 使用 `Path.resolve/lstat/st_dev/st_ino`，Windows 的 `O_NOFOLLOW` fallback 为 0；仅收集真实 `.py` 不能证明 bundle ancestor/source/fixture 不经 reparse。需要一个不依赖待验证 adapter 的最小 embedded bootstrap trust root。
- **Implications**:
  - 从 Gate roots 和 frozen owner manifest 生成 module/data closure，关键模块使用 `py` collection mode，避免手写易漂移列表。
  - 构建后运行 probe，逐个断言 `.py`、loader、origin、fixture digest、资源路径和 repository-path absence。
  - W3 批准后的第一个实现证据必须是最小 frozen spike，验证 exact `SourceFileLoader/spec.origin/co_filename`、无 PYZ duplicate、bootstrap handle-read 和 reparse/swap fail-closed；失败先重开 W3，不拖到完整打包阶段。

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
| external source via `module_collection_mode='py'` | 关键模块只以 bundle 中真实 `.py` 加载 | 可建立明确 loader/source identity | 需由 W3 新建合同、bootstrap root并实测 loader | **Proposed** |
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
- **Selected Approach**: `LockFileEx` 保护 persistent single-link lock file；candidate 用 `CREATE_NEW`、private ACL、file flush，随后从 candidate handle 相对 bound parent rename/replace，capture facts 后关闭 candidate 再 readback；replace 只在 owner-defined 排他资源 lock 下允许，恢复继续由既有 journal/LKG 状态机决定。
- **Rationale**: 崩溃释放、跨进程互斥、source identity 与 destination parent 都能绑定 handle。
- **Trade-offs**: 平台层不提供 expected-target CAS；不合作同用户进程仍可能竞态。需要更强保护的 owner 使用 immutable generation + journal/pointer。naming durability success boundary 需要 Windows 专属 ADR 与真实 power-cut/reboot 证明；不承诺 lock fairness。

### Decision 4：由 W3 建立 frozen-source/bootstrap 合同
- **Selected Approach**: PyInstaller `--onedir --windowed` + generated spec/hook；关键模块 `module_collection_mode='py'`，fixtures/data/assets 按生成 manifest 保持相对布局；embedded minimal bootstrap 在外置源码导入前绑定 bundle/source/fixture handles并铸造 `TrustedSourceAuthority`。
- **Rationale**: ADR-011 只拥有 Feature 5/UI DTO 与 composition，不是 source-loader trust authority；W3 必须明确建立 loader、bootstrap、manifest 和长期 build ownership 合同，不能把现状误称为 ADR-011 已批准。
- **Trade-offs**: 发行物包含可读源码且体积增加；这是当前能力合同的显式成本。

### Decision 5：按阻塞依赖分簇交付
1. Governance/ADR/ownership closure。
2. 最小 frozen bootstrap/SourceFileLoader feasibility spike；失败回到 W3。
3. 平台合同、Windows native primitives、POSIX adapter parity、power-cut 与反例 harness。
4. Parser + collaborative startup vertical slice。
5. 项目/资源/TMX persistence vertical slice。
6. TM lock/attestation/snapshot/recovery vertical slice。
7. 完整 frozen-source/build manifest/Qt resources。
8. clean Windows packaged E2E + macOS/Linux regression + release evidence。

## Governance Candidates

依据 `.kiro/settings/rules/governance.md:13-23`，以下不是普通本地 bug fix：

1. **ADR candidate W1 — Cross-platform rooted file authority, lock and durable publish boundary**
   - 改变共享 dependency/layer/composition boundary。
   - 定义 Windows live-handle identity、reparse、share mode、LockFileEx、candidate close/reopen lifecycle、非 CAS threat scope、handle-relative rename、NTFS power-cut durability success boundary 与稳定错误族。
   - 与 ADR-008/009/018/019 的 authority/atomicity/fail-closed 决策相交。
2. **ADR candidate W2 — Windows device-local private attestation representation**
   - 将 ADR-013/016 的 POSIX uid/mode/dev/inode 表示扩展为 TokenUser/owner SID、精确 DACL/AccessCheck、live VolumeSerial/FileId，并定义绑定 digest/phase/private/device-secret 的 cross-restart re-attestation 与 FileId reuse 反例。
   - 改变 frozen/persistent identity 与安全恢复边界。
3. **ADR candidate W3 — Windows frozen distribution ownership and source closure**
   - 新建 onedir/windowed build owner、embedded bootstrap trust root、`TrustedSourceAuthority`、generated source/fixture closure、bundle-root/loader contract 及发行门。
   - ADR-011 仍只拥有 Feature 5/UI DTO 与 composition；W3 是新的跨 Spec frozen/source-loader contract 与长期所有权，满足候选门槛。

用户已在 2026-08-25 将 W1/W2/W3 批准为 `APPROVED_FOR_BASELINE_NAMING`，因此本 Spec、依赖图和 amendment dispatch 可以稳定引用这些临时标签。正式 ADR 文档、编号、相交/取代关系和 Steering merge 仍由 Task 0.1/0.2 闭合；在此之前 design validation 与实现保持 **NO-GO**。

## Risks & Mitigations
- **Windows share mask 自阻塞** — 为 root/intermediate/source/target/candidate/lock 定义不同 handle profiles；运行占用目标、同进程二次打开和跨进程 replace 矩阵。
- **路径 proof 只在 NTFS 成立** — 探测 volume/file-system capabilities；首版只在通过完整 FileId/reparse/ACL/durability probes 的本地 volume 启用，其余 fail closed。
- **`SetFileInformationByHandle` failure residue** — candidate identity、journal phase、readback 和 LKG recovery 共同分类；每个 API failure point 注入测试。
- **pre-snapshot 被误当 CAS** — 仅允许 create-if-absent 或 resource lease 下 replace；记录不合作 writer threat scope，加入 instruction-boundary swap；需要强 CAS 的 owner 使用 immutable generation+journal/pointer。
- **FileId reuse 被误当永久身份** — FileId 只比较 live handles；receipt 绑定 digest/phase/private/device-secret并在重启后重新证明，覆盖 delete/recreate reuse。
- **process fault 被误当 durability** — W1 固定 NTFS success boundary，并用批准的 disposable VM/VHD/物理 lab 执行 forced power-off/reboot；缺证据保持 NOT_VERIFIED。
- **Windows ACL 与 POSIX mode 不等价** — ADR 固定 SID/DACL 规则，按 handle 重验 owner、DACL、inheritance、nlink；不使用 `chmod(0600)` 假通过。
- **同名 source 的 bundle 双份执行** — 关键模块使用 source-only collection；构建后检查 module loader/spec/origin 和不允许的 PYZ duplicate。
- **手写 manifest 漂移** — 从 Gate roots 与 owner manifest 生成闭包并在 CI diff/check；任何未声明动态 import 使 build gate 失败。
- **GUI windowed 无 stderr** — 提供受合同约束的 marker/log/exit probe；发布 E2E 同时运行真实 visible window 与 failure diagnostics。
- **范围过大导致半完成发布** — 每簇有 observable matrix，但只有最后 packaged clean-user matrix 全绿才将状态从 NOT_VERIFIED 改为 VERIFIED。

## References
- [Microsoft CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) — directory/reparse/share/write-through semantics。
- [Microsoft FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info) — Windows handle identity。
- [Microsoft GetFinalPathNameByHandleW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getfinalpathnamebyhandlew) — final resolved path evidence。
- [Microsoft LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex) — cross-process lock/crash release。
- [Microsoft SetFileInformationByHandle](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle) 与 [FILE_RENAME_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_rename_info) — handle-bound rename relative to directory handle。
- [Microsoft FlushFileBuffers](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers) — file buffer flush boundary。
- [Microsoft File Security](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights) — owner/DACL model。
- [Python 3.14 `os`](https://docs.python.org/3/library/os.html) — platform availability and Windows permission limitations。
- [Python 3.14 `ctypes`](https://docs.python.org/3/library/ctypes.html) — `WinDLL(..., use_last_error=True)` FFI/error handling。
- [PyInstaller spec files](https://pyinstaller.org/en/latest/spec-files.html)、[hooks](https://pyinstaller.org/en/latest/hooks.html)、[runtime information](https://pyinstaller.org/en/stable/runtime-information.html) — source/data collection and bundle paths。
- [Qt Windows deployment](https://doc.qt.io/qt-6/windows-deployment.html) — `qwindows.dll` plugin layout。
- [SQLite FTS5](https://www.sqlite.org/fts5.html) — real FTS5/trigram behavior。
- Internal discovery artifacts are non-authoritative inputs；正式 evidence 必须按 Design 的仓库相对 artifact key、manifest 与 checksum 生成，不能以本机绝对路径充当身份。
