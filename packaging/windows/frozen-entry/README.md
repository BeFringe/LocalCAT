# Windows frozen-entry candidate inputs

This directory contains the Task 1.5 target contract and its materialized
candidate-input lock. The lock binds the selected build search roots, pinned
CPython/PyInstaller inputs, expected native closure, exact Python C API target,
patch ownership/apply protocol, and expected PE import surface. The applied
patch and resulting PE belong to the Task 1.6 realized-build lock.

The producer runs with CPython 3.14 x64 and `pefile==2024.8.26`. Supply the
absolute paths for the selected local materialization, then run:

```powershell
$Replay = @{
    AuditPython = '<candidate audit venv>\Scripts\python.exe'
    VisualStudioInstall = '<Visual Studio Build Tools installation>'
    VisualStudioLayout = '<Visual Studio offline layout>'
    VsWhere = '<Visual Studio Installer>\vswhere.exe'
    WindowsSdkRoot = '<Windows Kits>\10'
    WindowsSdkVersion = '10.0.26100.0'
    MsvcVersion = '14.44.35207'
    PythonRoot = '<pinned CPython 3.14.7 root>'
    PyInstallerSdist = '<inputs>\pyinstaller-6.22.2.tar.gz'
    PyInstallerSource = '<inputs>\pyinstaller-6.22.2'
    PackagingDependencies = '<explicit build dependency site-packages>'
    ProbeWorkRoot = '<new empty parent>\stock-probe-source'
    SupportVerifiedOn = '2026-08-27'
    OutputPath = '<existing output parent>\candidate-input-replay.json'
}

.\tools\replay_windows_frozen_entry_inputs.ps1 @Replay
```

The replay wrapper copies the pinned pristine PyInstaller source, rejects any
pre-existing build cache, records the complete copied bootloader build-driver
aggregate before Waf can create build outputs, sanitizes the compiler environment, invokes
`vcvarsall.bat` with the exact requested MSVC/SDK versions, verifies the
resolved `cl/link/rc/mt` paths, and runs
`bootloader/waf all --target-arch=64bit -j1`. It retains raw stdout/stderr in the
new probe work root and emits portable canonical build evidence covering the
actual copied-source and pre-build build-driver aggregates, tool identities, arguments, sanitized
environment and result import table. The resulting stock `runw.exe` is used
only to prove the selected compiler's exact import-table delta. Its timestamped
PE bytes are not candidate authority; Task 1.6 owns reproducible customized PE
bytes and the realized-build lock.

`MATERIALIZED` means that the candidate inputs and target contract are
internally consistent. W3 reapproval changes the Task 1.5 governance state;
Task 1.6 then realizes and tests the custom entry against the mandatory matrix.

## Task 1.6：应用 native 闭包与 OS 服务边界

### 完整五补丁的开发验证

在锁定 MSVC x64/SDK 环境中运行：

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B -m tools.probe_windows_frozen_custom_runw `
  --pristine-source '<pinned PyInstaller sdist source>' --run
```

该诊断从经过输入锁校验的 pristine bootloader 库存重放完整五补丁；原生模板仅用于逐字节核对，不在重放后覆盖源码。`applied-sources.json` 记录有序补丁和完整输入/结果库存，其摘要进入 pre-link manifest。生成头文件只替换 manifest 模板中声明的摘要占位。

产物留在 `artifacts/windows/`；`--run` 分别执行捕获输出和标准句柄全 NULL 的同一 GUI PE。结果仍是诊断，不代替最终 PyInstaller 封装、E0～E11 全矩阵和双 clean final build。

修改原生模板后，可运行 `tools/render_windows_frozen_native_patches.py` 生成 `apply_patch` 请求，经审阅后更新四份原生源码补丁；构建不会自动修补陈旧补丁。输入生成阶段同时要求 existing owner 在真实 pristine 库存中存在，new owner 尚不存在。

### PyInstaller 封装组件验证

同一命令增加 `--packaging-dependencies '<显式构建依赖 site-packages>'` 后，会在 Waf 构建后继续执行真实 `EXE → PKG → COLLECT`，并将 `--run` 指向封装后的 GUI EXE。锁定 sdist 须与 pristine 目录相邻，文件名来自 candidate lock；PyInstaller Python 包重新从摘要匹配的 sdist 提取，不从已安装的 PyInstaller 取包。

封装子进程使用 `-I -S -B`、独占 cache/work/dist 及复制的构建依赖，不加入整个 site-packages 或仓库到搜索路径。构建依赖包括 `altgraph`、`packaging`、`pefile`（含 `ordlookup`/`peutils`）和 `pywin32-ctypes`，不会调用 Analysis 或收集 hooks。源码作为 DATA 收集，不产生 PYZ/字节码；空 CArchive 由 PyInstaller 正常写入。原生 DLL、源码与 fixture 在收集前后逐项比对原字节，最终目录拒绝额外条目。

PyInstaller 会对任意输入 XML 强制补入 Common-Controls，因此 spec 的 EXE 子类在 `Target.__postinit__` 的缓存检查和 assemble 前设置准确 manifest。封装后重新检查真实 PE 的 manifest、static/delay imports、windowed subsystem、时间戳和 Security/Debug Directory，不在输出 PE 上事后补丁。此扩展点只对锁定 upstream 实现有效。

candidate lock 的 `evidence_producer.custom_packaging` 绑定构建驱动/helper 源码、依赖文件库存摘要及 manifest/布局合同。输入生成器与重放脚本要求显式依赖目录；该目录只是读取来源，其绝对路径不进入 candidate 身份。缺少绑定的旧锁仍可供历史诊断读取，但不得进入此封装路径；当前工具/依赖在 Waf 前与候选比较，复制后的依赖和子进程驱动再与同一候选比较，不重新计算期望值放行变化。

`packaging/inputs.json` 使用相对路径，记录实际包源码、依赖、驱动、spec 和 payload 摘要，并关联 candidate 与 packaging target 摘要；父进程将输入摘要传给子进程并复核返回结果与现场 dist。其分类为 **CANDIDATE_BOUND_PACKAGING_INPUTS_NOT_W3_APPROVAL**：构建输入绑定不等于 realized-build lock、完整 E0～E11 矩阵或 W3 批准。该组件跑通或两次字节相同均不能单独关闭 Task 1.6。

### 从干净提交生成 post-build 绑定

在锁定 vcvars 环境、无修改或未跟踪输入的独立工作树中运行：

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B -m tools.build_windows_frozen_spike `
  --expected-commit '<调用方选定的完整提交>' `
  --expected-candidate-lock-sha256 '<已批准 lock 文件的 SHA-256>' `
  --pristine-source '<pinned PyInstaller sdist source>' `
  --runtime-root '<pinned CPython root>' `
  --packaging-dependencies '<显式构建依赖 site-packages>' `
  --vswhere '<Visual Studio Installer>\vswhere.exe' `
  --output '<该工作树>\artifacts\windows\<新的构建目录>'
```

编排器在构建前后检查同一 clean HEAD，并把 `tools/` 与 `packaging/windows/frozen-entry/` 的实际原始字节与 Git archive 比较；被忽略的额外构建输入也不能绕过检查。它独立重放补丁、runtime/source/fixture 输入和生成头文件，再逐字节核对本轮实际构建目录，直接解析最终 PE 的 imports、manifest、Debug/Security Directory 与完整空 CArchive。旧 dirty 诊断不能事后升级为 clean build。

仅净化外层 `INCLUDE`/`LIB` 不足以固定编译输入：Waf 的 MSVC 自动探测会重新加入 VS 辅助目录等搜索根。补丁关闭该重探测并显式设置锁定工具与目录；编排器同时核对 Waf 有效配置和实际编译／链接命令，避免只验证启动环境而遗漏真正的搜索路径。

`release.json` 位于 dist 外，绑定 clean commit/source、candidate、pre-link、runtime manifest、applied source、最终 PE 与完整相对 dist 库存，不包含机器绝对路径、时间或诊断日志摘要。编排器输出的 `release_sha256` 必须由调用方在产物外保留；`verify_release_binding` 要求显式传入该摘要、commit 和 candidate，不能信任待检目录自己提供的 receipt。完整自洽地替换 PE 与清单仍应被原外锚拒绝。

本单元分类为 **BUILD_BINDING_NOT_W3_ACCEPTANCE**。它提供 post-build 绑定，不将源码 API 正则投影变成 realized C API 证明，也不授予 E0～E11 或完整产品发行通过。构建前后核对针对受信构建主机上的输入漂移，不声称能防御已被控制的 OS 或编译器。

### 最终 PE 的实际 API 绑定

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B -m tools.trace_windows_frozen_api_binding `
  --dist '<最终 dist>' --release '<dist 外的 release.json>' `
  --expected-release-sha256 '<调用方保留的 release 摘要>' `
  --expected-commit '<完整 clean commit>' --expected-candidate-digest '<candidate input 摘要>' `
  --cdb '<Windows SDK>\Debuggers\x64\cdb.exe' --profile normal
```

对同一发行物再运行 `function-null` 和 `data-null`。观察者从最终 PE 的 `.pdata` 函数边界完整解码 binder 的调用、槽位和失败分支，并对照随包 Python DLL 的直接导出；正常路径逐项检查实际返回地址、写入与成功返回前的全部槽位。NULL profile 只改变新子进程中的一个解析返回值，验证它在初始化前拒绝。

稳定绑定表记录 RVA；原始调试轨迹保留实际加载地址。外部 release 锚在观察前后核对完整 dist，C 签名仍由锁定 headers 和干净编译承接。结果分类为 `REALIZED_E8_BINDING_NOT_W3_GATE`，不替代其余阶段验证。仅精确记录已锚定零时间戳 EXE 的 CDB 时间戳诊断，不忽略其他模块或断点错误。

### 源码编译输入与执行对象

`tools.trace_windows_frozen_source_execution` 使用与 API 观察者相同的 `--dist`、`--release`、三个外锚和 `--cdb` 参数，并增加 `--prelink '<同一构建的 prelink.json>'`。它核对已绑定的 Python DLL 调用点，捕获解释器启动源码、bootstrap 与 critical module 真正传入编译器的完整字节及结尾 NUL，再关联返回的代码对象与实际 `PyEval_EvalCode` 入口。

输出记录每份捕获字节的摘要、观察器与解析器库存、原始轨迹和输入前后复验。结果为 `EXACT_SOURCE_EXECUTION_NOT_FULL_W3_GATE`：证明这些编译输入及代码对象进入执行，不声称覆盖全部语句、未执行分支、其他求值调用或路径访问闭包；保留句柄与交换攻击仍须独立验证。

### 启动前 payload 与目录污染反例

`tools/verify_windows_frozen_payload.py` 可从任意 CWD 调用，参数为 `--dist`、`--release`、`--expected-release-sha256`、`--expected-repository-commit` 和 `--expected-candidate-input-digest`。工具只修改每例新复制的样本，核对精确差异、真实退出及 marker，并复验原始发行物和观察器；证据包含独立重放快照。正常对照使用已有 System32 作为 checkout 外 CWD，污染目录只在独立证据目录创建。

此矩阵覆盖启动前替换、缺失、额外源码／缓存／DLL 和固定导入诱饵；无重新签入的源码追加首先由长度检查拒绝，同长度变化另验摘要。`.pyd` 诱饵是无效 PE 的发现探针，不是 DllMain 执行探针；结果 `PRELAUNCH_PAYLOAD_EVIDENCE_NOT_FULL_W3_GATE` 不替代交换时序、完整 parser 或系统解析验证。

### 实际封装入口的时序观测

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B -m tools.trace_windows_frozen_entry `
  --diagnostic-build '<本轮封装诊断目录>' --cdb '<Windows SDK>\Debuggers\x64\cdb.exe'
```

该观察者只启动指定的新样本；先核对当前 candidate、PE、Python DLL 与完整 dist 库存，再以模块限定的导出符号加偏移设置 CDB 断点。输出保存观察者、脚本和输入摘要、原始日志、marker 顺序及真实 debuggee 退出码。`--profile mimalloc-stats` 只在新子进程中设置统计选项，用于检查退出支线；两种 profile 均清除继承的 Python、Qt、mimalloc 和调试符号路径覆盖，不修改用户环境。

结果分类始终是 `OBSERVER_NOT_CALL_CLOSURE_PROOF`。断点告警、缺少 Python DLL entry/完成/退出事件或非零退出不能报告完整成功轨迹；无命中不表示不可达，初始调试断点也不能证明更早的 static import 解析。实际代码根、搜索参数和仍待处理的合同缺口见 [入口调用点审查记录](entry-callsite-audit.md)。

### 系统入口边界

v3 输入绑定应用系统 API 入口和非系统 dispatcher 合同。
`BCryptGenRandom(NULL, flags=2)` 使用解释器正常的系统随机源。

应用自有的 native entry、Python DLL/随包依赖、实际源码和 fixture 仍须完整证明。
系统 API 入口的搜索策略仍受审；CWD/PATH、同名 DLL 或应用可控的重定向不获放行。
准确边界与真实 custom entry 验收见 `w3-custom-entry-plan.md` §5.2。

精确的应用系统加载目标另见该计划 §5.4 和 target 的 `application_system_calls`：包括 E0.5 CRT 固定探测、CRT 终止策略查询及 E1 后的 mimalloc 初始化/失败退出支线。CRT 终止策略查询包含 E1 未成功的失败返回，仅允许固定 AppModel API-set 和 `0x800`，没有后备目标或默认搜索回退；其系统解析证明不得依赖 E1 成功。生成器及当前 custom-entry 消费者都核对完整调用清单、阶段、flags 和有条件回退；重新计算 JSON 摘要不允许扩大目标。旧 v3 输入若缺少此清单，须由生成器重新 materialize，不能补默认值继续执行。该声明不替代系统目标解析或最终候选反例证据。

以下可选工具记录系统文件诊断快照，独立于 candidate 输入。
诊断专用 target 采用 `localcat.windows-system-provider-target.v1` 的显式 roots 格式：

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B -m tools.windows_frozen_system_profile `
  --target '<diagnostic work>\roots-target.json' `
  --output '<existing output parent>\explicit-system-roots.json'
```

该命令只采集诊断声明的 roots；系统内部 loader flags 与应用 dispatcher flags 分别记录。

### Provider 选择的只读诊断

`tools/probe_windows_cng_selection.c` 先按冷启动实际请求查询 interface 6 的首个
function（function/provider 为 NULL、flags 为 0），再查询该 function 的 provider
解析与注册记录；失败或空结果不会回退到硬编码 RNG。
同时记录初始化分支相关的进程查询响应；它不调用 RNG、不显式加载 provider，
也不修改系统配置。在与 candidate 一致的 MSVC x64/SDK 构建环境中，
从独立诊断工作目录运行（所有占位路径均须替换为绝对路径）：

```powershell
cl.exe /nologo /W4 /WX /MT /O2 /Brepro /guard:cf /TC '<repository>\tools\probe_windows_cng_selection.c' /Fe:cng-selection.exe /Fo:cng-selection.obj /link /Brepro /GUARD:CF
& '<candidate audit venv>\Scripts\python.exe' -B '<repository>\tests\test_windows_cng_selection.py' --probe '<diagnostic work>\cng-selection.exe'
```

诊断输出使用 v3 schema，拒绝旧 mode-query 数据：首个查询的实际 class 是
`0x56`，`query86_*` 字段保存其原始响应。仅解释当前 candidate 已观察的响应，
不模拟其他负 NTSTATUS 的全部分支。fallback 的
[`PROCESS_EXTENDED_BASIC_INFORMATION / IsSecureProcess`](https://learn.microsoft.com/en-us/windows/win32/procthread/isolated-user-mode--ium--processes)
有公开定义，但首个私有查询仍是 candidate-bound 事实，不是通用稳定 API。

退出成功只表示诊断结构有效。测试使用合成 fixture；查询结果仅描述观测时的状态。

## 历史 v1 terminal diagnostic replay

以下仅用于在原 v1 输入及相应源码快照上重放旧 NO-GO；工具拒绝 v2 及当前 v3 lock。

The custom-entry producer stops before candidate construction when a hard
assertion cannot satisfy the approved Task 1.5 contract. The diagnostic probe
does not grant authority and its loader notification cannot prove closure. It
records an observed load, pinned-binary disassembly, and pristine PyInstaller
source facts as separate evidence classes. The `MODULE` records are loader
notification paths followed by diagnostic path reopen identity/digest checks;
they are not retained backing-file handles and do not satisfy actual-module
reproof.

Run from the repository root with a new output directory:

```powershell
& '<candidate audit venv>\Scripts\python.exe' -B `
  .\tools\audit_windows_frozen_custom_entry.py `
  --repository-root $PWD `
  --candidate-lock .\packaging\windows\frozen-entry\candidate-input.lock.json `
  --matrix-contract .\packaging\windows\frozen-entry\custom-entry-matrix.json `
  --pyinstaller-source '<inputs>\pyinstaller-6.22.2' `
  --cpython-root '<pinned CPython 3.14.7 root>' `
  --vcvarsall '<Visual Studio Build Tools>\VC\Auxiliary\Build\vcvarsall.bat' `
  --msvc-version 14.44.35207 `
  --windows-sdk-version 10.0.26100.0 `
  --dumpbin '<Visual Studio Build Tools>\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64\dumpbin.exe' `
  --output '<ignored artifact root>\task1-6-custom-no-go-replay'

& '<candidate audit venv>\Scripts\python.exe' -B `
  .\tools\validate_windows_frozen_custom_entry_no_go.py `
  --repository-root $PWD `
  --evidence '<ignored artifact root>\task1-6-custom-no-go-replay\custom-entry-no-go.json' `
  --raw-artifact-root '<ignored artifact root>\task1-6-custom-no-go-replay' `
  --candidate-lock .\packaging\windows\frozen-entry\candidate-input.lock.json `
  --matrix-contract .\packaging\windows\frozen-entry\custom-entry-matrix.json `
  --schema .\packaging\windows\frozen-entry\custom-entry-no-go.schema.json `
  --cpython-root '<pinned CPython 3.14.7 root>' `
  --pyinstaller-source '<inputs>\pyinstaller-6.22.2' `
  --vcvarsall '<Visual Studio Build Tools>\VC\Auxiliary\Build\vcvarsall.bat' `
  --msvc-version 14.44.35207 `
  --windows-sdk-version 10.0.26100.0 `
  --dumpbin '<Visual Studio Build Tools>\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64\dumpbin.exe' `
  --run-negative-self-tests
```

`NO_GO` deliberately returns a non-zero producer status. The validator must
emit `VALID_NO_GO`. It independently rebuilds and reruns the probe, replays the
PE/IAT/disassembly and pristine-source facts, and verifies every raw sibling
against the current lock and scoped working-diff identity. Before trusting PE
or subprocess-derived facts, it verifies the executing Python/base-runtime,
loaded `pefile` source/METADATA, and the MSVC/SDK `bin`/`include`/`lib` input
aggregates actually reachable by the build. The final probe environment replaces the broad
`vcvarsall` search lists with the exact locked MSVC/SDK x64 `INCLUDE`/`LIB`
projection and clears `LIBPATH`; both producer and validator reject any later
out-of-scope search root. Strict JSON parsing rejects duplicate keys at every
nesting level in producer and validator semantic inputs, and the repository
schema is applied fail-closed. The negative self-tests cover producer candidate
lock/matrix duplicates, evidence duplicate-key last-wins, schema-invalid
self-consistent evidence, producer-TCB tampering, toolchain aggregate tampering,
and an out-of-scope compile include root. Two clean replays
compare `replay_projection_digest`; each run retains its own content digest for
raw compiler/probe outputs.
