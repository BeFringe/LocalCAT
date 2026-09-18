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

v3 输入绑定应用系统 API 入口和非系统 dispatcher 合同。
`BCryptGenRandom(NULL, flags=2)` 使用解释器正常的系统随机源。

应用自有的 native entry、Python DLL/随包依赖、实际源码和 fixture 仍须完整证明。
系统 API 入口的搜索策略仍受审；CWD/PATH、同名 DLL 或应用可控的重定向不获放行。
准确边界与真实 custom entry 验收见 `w3-custom-entry-plan.md` §5.2。

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
