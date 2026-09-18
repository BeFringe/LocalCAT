# W3 最小应用的 loader 调用闭包审查

## 结论与适用边界

本记录对应同一最终 windowed PE、随包 Python DLL、VCRuntime 和保留源码，解释应用拥有的装载调用根及其切断条件。它补充 [入口调用点审查记录](entry-callsite-audit.md)，不是 `DYNAMIC_ROOTS_DECLARED` 或全部 W3 的通过凭据。

**本文保留 clean build `d` 的历史调用审查；修订后的 `f`／`g` 候选已另行完成最小 spike 验收。** 本文外锚对应的旧 candidate 尚无既有 EXE 静态 CRT 本地化分支声明，因此旧成品不能据此取得 E4 PASS。该分支在非 UTF-8 ANSI 代码页下、E1/Python/worker 之前可达；本机自然返回 65001 时走捷径，因此先前正常轨迹没有显示它。受控替换一次真实 `GetACP` 返回为 1252 后，实际新增请求仍是 `LoadLibraryExW("api-ms-win-core-localization-l1-2-1", ..., 0x800)`，返回已加载的 System32 KERNELBASE。本轮没有观察到随包或 ambient DLL 误加载。已批准的补录仅涉及同一既有 OS 信任边界内的精确调用清单及其证据；当前成品、回退反例与 `d`→`f` 输入／机器码桥接见[最终验收记录](spike-validation.md)，不要求用户改变区域设置。

必须区分三类事实：

- **二进制枚举**：本轮完整 `dumpbin /disasm:bytes` 中，Python 的 loader IAT 引用是 11 处直接 CALL，EXE 是 5 处；VCRuntime 另有一个导出 JMP 跳板。不是只检索预设的 11 个 RVA。
- **源码及机器码推导**：固定参数、初始化顺序、实际保留源码的执行行为，说明未批准入口哪些已被切断、哪个 CRT 入口实际仍可达。下文分别标记，不能把“固定目标”写成“已获批准”。
- **运行观察**：配对 loader 轨迹、编译输入、故障和污染反例只证明各自实际运行；未命中不能证明某个调用不可达，事后模块清单不能补授加载前 authority。

范围为受信构建工具产生的 exact candidate，不包含被控制的 Windows、调试器或编译器，不审计 Windows 系统服务内部的二次加载。后续加入 Qt、PySide6、SQLite 或更多启动源码时，必须重新审计应用闭包。

## 1. 输入和重放锚

本次审查使用 clean build `d` 的外部绑定：

| 对象 | SHA-256 |
|---|---|
| 外置 `release.json` | `de66697580de3c97c4979fd4f1e30dae683aaf6a1afb6e0b9b028572d8f176d8` |
| candidate input digest | `f9d09f919d008b246f8988cf71ab3c36027e69ad81a3bda1763d6bb19b4e7b01` |
| 最终 `localcat-spike.exe` | `41f5ffae97c458040d23ad4fa3afcfea43f8ecb0ca1b7e196299b8fcd63ec2f5` |
| `python314.dll` | `0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700` |
| `vcruntime140.dll` | `d1f4225df2cd877dbf130d5668a021dce3f94118455ff5ec952061c30afc9ce7` |

release 中的 clean commit 是 `6e3b6f780b845622ab8c32b07ffce66744d9dc79`。这里记录 hash 是绑定审查产物，不是把历史提交升级为设计要求。审查其他产物时不得只复用本文 RVA。

原始证据在 [`artifacts/windows/application-loader-closure-20260918/`](../../../artifacts/windows/application-loader-closure-20260918/)，含 `replay.py`、`evidence.json` 和三个 binary 的 imports/headers/exports/disassembly 输出。脚本在前后调用现有 release validator，要求上述三个外锚；不改发行文件。使用锁定 MSVC `14.44.35207` 的 `dumpbin.exe`，原文件 SHA-256 为 `12a1cd87238bd66dfdb788b4fcdcb91ce4b3f81236aab12a7a29e5ae1d85af50`。

| 完整反汇编输出 | SHA-256 |
|---|---|
| `localcat-spike.exe-disasm-bytes.stdout` | `3e9f4a7dc990ffcbfd241cbc3ee083e57125f1331e7cd10c8d8f2bad1cf9577c` |
| `python314.dll-disasm-bytes.stdout` | `1fcabfd5e91b29d8cd4582191b82a50deceb3794b0d26ff91aa6a7f6d834ed70` |
| `vcruntime140.dll-disasm-bytes.stdout` | `d0a15a8b3176d09be1bfa0c21bc640438eaf7731794565df1c34e9fe7016d851` |

这些输出含本机绝对路径，因此摘要是本轮原始证据绑定，不要求搬到另一台机器后文本摘要相同。以同一 binary 重放并核对 RVA、指令、IAT 和参数；不是比较运行时 ASLR 地址。`evidence.json` 的 `iat_references` 记录每个实际引用及输出行号，`sources` 记录逐文件原始大小和摘要。

源码参照为 `Python-3.14.7.tar.xz`，原始 SHA-256 `3b48dac8fb59f62eaa67ac83c1eb12bda1b7a08406dd286e252c11a66be27f81`；重放逐项比较解压文件与 archive member。关键 loader 指令、目标字符串及参数另从 exact DLL 核对，MSVC/SDK 安装所带 CRT 源码用于解释这些机器码。源码摘要是参照绑定，不以版本名代替逐调用点审查，也不要求为本单元重新构建官方 Python DLL。

## 2. 全部已枚举 loader IAT 引用

EXE image base `0x140000000`，Python/VCRuntime image base `0x180000000`；下表均为 RVA。Python loader IAT 分别为 `0x3242E0 / LoadLibraryW`、`0x324558 / LoadLibraryA`、`0x3245B0 / LoadLibraryExW`；EXE 为 `0x1F0F0 / LoadLibraryExW`。

| Python RVA | 实际入口／参数来源 | 源码根与本次处置 |
|---|---|---|
| `10EF3D` | `LoadLibraryW("api-ms-win-core-file-l2-1-4")` | `pycore_fileutils_windows.h:48–75` 的缓存解析，经 `posixmodule.c:2257` 的 `win32_xstat_impl`、`:5327/:5388` 文件类型／存在检查等调用；不仅是 `nt.stat` 一个名字。未批准，按 §4 切断文件发现和直接调用。 |
| `18B86A` | `LoadLibraryA("kernelbase.dll")` | mimalloc `prim/windows/prim.c:124`；DLL CRT 初始化中的 memory 初始化；按已批准系统入口处理，要求 E1 先建立。 |
| `18B8A8` | `LoadLibraryA("ntdll.dll")` | 同文件 `:132`，同阶段。 |
| `18B8DD` | `LoadLibraryA("kernel32.dll")` | 同文件 `:138`，同阶段。 |
| `1B9602` | PYD pathname，flags `0x1100` | `dynload_win.c:204–237` 的 `_PyImport_FindSharedFuncptrWindows`；与 dispatcher 的 `0x900` 不同。未批准，按 §4 切断扩展导入。 |
| `1B9921` | Python DLL 邻接 `python3.dll`，flags `0x1000` | `dynload_win.c:170–174`；`_Py_CheckPython3` 的第一条尝试。未批准，依赖同一个扩展导入切断条件。 |
| `25940E` | `python3.dll`，flags `0x200` | `dynload_win.c:181–183` 的 application-dir 尝试；编译器拆出的冷分支，不能因为远离主函数而遗漏。 |
| `25949C` | prefix 下 `DLLs/python3.dll`，flags `0x1000` | `dynload_win.c:193–196` 的兼容尝试；也是上述冷分支。 |
| `28A557` | `LoadLibraryW("SHELL32")` | `posixmodule.c:14753` 的 `os/nt.startfile` 延迟绑定。未批准；导入 `nt` 不执行该函数，实际保留源码未调用它。不能借上游 KnownDLL 注释自行批准。 |
| `2B059F` | `LoadLibraryA("psapi.dll")` | mimalloc `prim/windows/prim.c:454–460`；统计退出路径中缓存为空时；含失败退出，仍按既有系统入口合同。 |
| `2B0660` | `LoadLibraryA("bcrypt.dll")` | 同文件 `:555–560` 的随机源 helper，缓存为空时；weak-seed 重试只是一个上游根，不是所有到达路径的条件。 |

`Python/fileutils.c:2245–2306` 的两个 `api-ms-win-core-path-l1-1-0.dll` 调用在 `MS_WINDOWS_GAMES && !MS_WINDOWS_DESKTOP` 分支，不在当前 desktop binary 的上述 IAT 引用中。源码 grep 的条数不能直接当作最终 binary 调用数。

| EXE RVA | 源码根／入参 | 阶段与边界 |
|---|---|---|
| `6847` | `localcat_native_closure.c` 的已证明绝对路径，`LoadLibraryExW(...,0x900)` | 唯一非系统 dispatcher；保留句柄证明和 inventory 先于调用，返回后 actual-module reproof；返回路径元数据不是加载前证明。 |
| `A24D` | MSVC `winapi_downlevel.cpp:88`，`0x800` | E0.5 的 critical-section/FLS 固定候选；`api-ms-win-core-synch-l1-2-0`、`api-ms-win-core-fibers-l1-1-1`，各自后备 `kernel32`。 |
| `A285` | 同文件 `:99–101`，flags `0` | 仅前次失败且错误为 87、名字不是 `api-ms-` 的兼容分支；不能把 flags=0 视为已有 E1 保护。 |
| `17061` | SDK UCRT `winapi_thunks.cpp:210`，`0x800` | 已记录的 FLS2 `api-ms-win-core-fibers-l1-1-2 → kernelbase`、退出 AppModel API-set；**还包括本轮发现的 pre-E1 multibyte 初始化：`api-ms-win-core-localization-l1-2-1 → kernel32`，已批准最小清单补录，仍须新 candidate 绑定与验收。** |
| `170B5` | 同文件 `:222–225`，flags `0` | 兼容分支同时排除 `api-ms-` 和 `ext-ms-`；AppModel 没有真实 DLL 后备，故不能由它进入这条回退。 |

共享 CRT thunk 的完整名称表不是允许列表：UCRT 同源还实现 locale、windowing 等包装器。只有实际初始化、终止等根与已批准固定候选可在此解释，不能从共享函数存在推出全部名称可用。flags=0 后备的系统解析和应用可控重定向仍由独立系统入口证据裁决。

### VCRuntime 不能被“零直接 CALL”省略

`vcruntime140.dll` 导出 `__vcrt_LoadLibraryExW`，RVA `5BC0` 的实际指令是 `48 FF 25 79 A4 01 00`，跳到 IAT `20040`；`5BB0` 类似导出 `__vcrt_GetModuleHandleW`。这是七字节 JMP，不会被只认六字节 `FF 15` 的枚举器发现。

本轮完整 dumpbin 对 loader IAT 的引用只有该跳板；没有找到其内部直接 CALL 或 image-VA 指针引用。EXE/Python 的实际 imports 不引用此导出，现有 `GetProcAddress` 根也不查询它。因此不能把它算作当前启动的另一条已执行加载；也不能仅凭“零直接 CALL”证明所有间接入口不存在。VCRuntime 本身由 dispatcher 先加载，并以 exact 摘要和递归 system imports 纳入闭包。

## 3. 初始化、函数指针和退出根

### CRT 与 DLL entry

EXE 的真实入口是 RVA `9100`，不是 `wWinMain`。MSVC `exe_common.inl` 的 `__scrt_common_main_seh` 初始化 VCRuntime/UCRT，运行初始化表，再调用 `wWinMain`；正常返回及 launcher 拒绝的返回都进入 CRT exit。`winapi_downlevel.cpp:20–25` 固定 FLS／critical-section 候选，`initialization.cpp:104–119` 将锁和 PTD 初始化连到它们。E1 只约束之后的装载，不能覆盖这些更早调用。

Python DLL 的入口为 RVA `1E4F74`。不能只审 `PC/dl_nt.c` 的用户 DllMain：`Objects/obmalloc.c` 静态纳入 mimalloc；`Objects/mimalloc/init.c:656–668` 在 `.CRT$XIU` 放入 `_mi_process_init`。实际 DLL 在 `1E4D25` 调用 `_initterm_e`，参数区间 `[324D80,324D90)` 含指针 `18018B5BC`；该函数执行 mimalloc process-load、注册终止回调并初始化 memory。TLS 目录的 callback 表 RVA `324DA0` 首项为 NULL；这与 `18C2CB` 用 `FlsAlloc` 注册线程退出回调是两回事。

VCRuntime 入口 RVA `1BA60`，无 TLS callback 目录；其已观察 FLS 注册在 `5AFB`，参数为本 DLL 内 `5850`。这是真实回调根，不能把 FLS 错写成第三方 DllMain。源码 `initialization.cpp:104–165` 将 process/thread attach、detach 连到锁和 PTD 的建立／释放；不从环境构造 DLL 名或注册外部 loader 回调。

### 已收敛的 CRT resolver 根

完整 EXE 反汇编中的 VCRuntime resolver `A1C8–A317` 只由五个固定 wrapper 调用：`A338 / FlsAlloc`、`A382 / FlsFree`、`A3CA / FlsGetValue`、`A419 / FlsSetValue`、`A475 / InitializeCriticalSectionEx`。它们接收各自固定名称和候选表，服务初始化、线程局部状态及释放，不接收 payload 提供的模块名。

UCRT resolver `16FF4–171A6` 有四个实际 wrapper 根，不是源码宏表中的所有函数：

| wrapper 及 resolver CALL RVA | 实际来源与控制条件 |
|---|---|
| `171A8 / 171DB`，AppPolicyGetProcessTerminationMethod | `exit.cpp:256–288` 的正常／失败终止；固定 AppModel API-set，无真实 DLL 后备。 |
| `173C0 / 173F2`，FlsGetValue2 | PTD 读取 `14A54`；固定 fibers API-set／kernelbase。 |
| `17238 / 17282`，LCMapStringEx | 宽字符比较的 locale 分支，以及下面**确实可达**的 multibyte 初始化；不能合并排除。 |
| `1732C / 17365`，LocaleNameToLCID | LCMapStringEx 缺失时，`172E8` 进入 LCMapStringW 后备前的 locale 转换。 |

静态 EXE CRT 的初始 C locale 会使 `wcsicmp.cpp:54`、`wcsnicmp.cpp:66`、`towlower.cpp:59` 选择无 locale 映射的字符转换；当前 native 不调用 setlocale，Python 使用另一份共享 CRT。**这不能排除 multibyte 初始化**：它读取系统 ANSI 代码页，与上述宽字符比较的 locale 条件不同。

### 非 UTF-8 ACP 的具体遗漏及最小差异

EXE 的 `_initterm_e` CALL `8FE2` 遍历 `[1F310,1F348)`；其中 `1F330 → 1A130 → 15D50` 对应 `initializers/multibyte_initializer.cpp:15–20` 注册的 `__acrt_initialize_multibyte`。`mbctype.cpp:919` 以 `_MB_CP_ANSI` 调用初始化，`:566–569` 真正读取 `GetACP`。实际 CALL `15755`、返回 `1575B`；随后 `1586B` 比较 `65001`，相等就跳到 `159BE`，否则在 GetCPInfo 成功时进入大小写映射。映射 CALL `1592F / 15968 → 1A08C → 19D58 → 17238` 最终到 loader `17061`。这是一条 CRT initializer 表和计算式调用已经落到具体固定目标的根，不是仅有源码字符串的猜测。

实际 PE 的 RT_MANIFEST 与打包 `APPLICATION_MANIFEST` 均只有 asInvoker／longPathAware，没有 `activeCodePage=UTF-8`。Python 的 UTF-8 配置发生更晚，也不改变这里的 CRT GetACP 返回。

经用户批准的精确补录差异如下：

- `LCMapStringEx` 和 `LocaleNameToLCID` 均使用 UCRT 固定候选 `api-ms-win-core-localization-l1-2-1 → kernel32`（`winapi_thunks.cpp:101/:103`），首次尝试 flags `0x800`。候选循环**只在模块装载失败时**前进；拿到第一个可用模块后只查询一次 GetProcAddress，不因 export 缺失而遍历下一个候选（`:271–300`）。
- flags `0` 兼容尝试仅在 flags `0x800` 失败、错误为 87 且名字既非 `api-ms-` 又非 `ext-ms-` 时发生（`:208–228`）；这里因此只可能对真实 `kernel32` 后备尝试，不会对 localization API-set 以 flags `0` 加载。沿用既有系统解析／重定向审查边界，不能因为上游称 KnownDLL 就省略它。
- LCMapStringEx export 不可用时，wrapper `:706–724` 调用已静态导入的 LCMapStringW，并先经 LocaleNameToLCID wrapper 转换；后者也不可用则进入 `:741–751` 的 downlevel 转换，不再引入另一个 loader 目标。此后备链有源码及机器码对应，本轮未声称真实触发。

原始成对观察为 `acp-control-observed.*`、`acp-1252-observed.*`，重放入口 `observe_acp.py`。自然 control 在 `1575B` 返回 `0xFDE9`；另一新进程只将**第一次真实 GetACP 返回**的 EAX 改为 `0x4E4`，不改系统 locale、磁盘输入或调用点。它随后实际请求 localization API-set（flags `0x800`），返回地址落在原始 ModLoad 中的 System32 KERNELBASE，解析 LCMapStringEx 后才出现 E1；两者均自然到 SPIKE_COMPLETED 并进程退出 0，release／完整 dist 前后复证不变。受控观察不是另一台非 UTF-8 系统的完整实测，返回 host 路径也不代替独立 pre-entry 系统证明。

| 成对原始 CDB stdout | SHA-256 |
|---|---|
| `acp-control-observed.stdout` | `4e63653bdaf3f30f142a402cc2fab24a3940f1e8a9c070696b6b55ba2b79c637` |
| `acp-1252-observed.stdout` | `1ce93cf0208f9458f50dab1609c8e55964fae544f73b9f947ede8c7f63556253` |

该根不是新 bootstrap／worker 引入：旧 `b` 成品 SHA-256 `e0302d378e61e79e4f7eb8c16d88fd7ebc595380aa92eb8371a3c6c6c364d2cf` 与本次 `d` 的初始化 callloop、GetACP／codepage 分支、resolver、locale wrapper、整个上述 initializer 表逐字节相同；`evidence.json.preexisting_crt_roots` 记录范围和原始摘要。

### GetProcAddress 不是任意字符串授信

实际 EXE 有 41 个、Python 有 19 个 `GetProcAddress` IAT 引用；VCRuntime 无此导入。除下面单独指出的扩展入口外，已对应的名称是固定源码／只读数据，不取 CWD、PATH、argv 或 manifest 中的任意函数名。

| binary / RVA | 返回函数指针的来源与用途 |
|---|---|
| EXE `229B–272F` | E8 binder 的 38 项：37 个批准 C API 函数及 RuntimeError 数据导出。独立 E8 解码与实际调用／返回／最终槽位证据负责完整绑定，不靠本文源码正则。它们不包括任意 loader API，但 `PyImport_ImportModule` 等的调用参数仍要审。 |
| EXE `A2FF` | VCRuntime 固定候选及名称表；服务于 FLS／critical-section 包装器，不接受应用任意名字。 |
| EXE `17192` | UCRT 共用固定名称表；必须按可达包装器区分初始化、退出和其他未授权候选，不能将整个表一并批准。 |
| EXE `11CFB` | `CorExitProcess`；`exit.cpp:84–90` 只通过 `GetModuleHandleExW` 查询既有 `mscoree.dll`，不是主动加载 CLR。 |
| Python `EB233` | traceback 的固定 `GetThreadDescription`，从已加载 kernelbase 查询。 |
| Python `EB5C5 / EB5E5` | `_threadmodule.c:2743/2747` 的固定 Get/SetThreadDescription；也是查询既有 kernelbase，不调用 loader。 |
| Python `10EF63` | 上述文件状态 API 的 `GetFileInformationByName`；由 §4 的文件发现／显式调用切断。 |
| Python `18B882 / 249AA0 / 18B8C0 / 18B8F5 / 18B90C / 18B923 / 18B93A` | mimalloc 的 VirtualAlloc2FromApp／VirtualAlloc2、NtAllocateVirtualMemoryEx 与四个 processor/NUMA 查询函数。`249AA0` 是 VirtualAlloc2 的冷后备，不是另一次任意符号查询。 |
| Python `1B968E` | **非固定函数名**：由扩展模块短名形成 `PyInit_*` 等，再在已加载 PYD 查询并调用初始化函数；必须由扩展导入上游切断，不能以函数名白名单或 post-load inventory 补救。 |
| Python `28A56C` | 固定 ShellExecuteW，仅在 startfile 延迟加载成功后查询。 |
| Python `28BD90` | 固定 NtQueryInformationProcess，来自 `posixmodule.c:9515` 的已加载 ntdll 查询。 |
| Python `2B05B4 / 2B0675` | 固定 GetProcessMemoryInfo／BCryptGenRandom，承接上述 PSAPI／bcrypt 缓存。 |
| Python `2D0295 / 2D0395 / 2D0D8E` | winreg 的 RegDisable／Enable／QueryReflectionKey；仅查询既有 advapi32，模块导入不等于调用这些方法。 |

“查询既有系统模块”与“允许新的系统加载”不同；这里不据此增加应用系统目标，也不进入这些系统函数的内部实现。

## 4. 未批准入口在真实最小初始化中的切断点

### 不是靠初始化后的检查补救

`localcat_frozen_bootstrap.c:390–436` 明确配置 isolated、no environment/site/user-site/bytecode、safe path、不解析 argv，清空 xoptions/warnoptions，禁用 faulthandler/tracemalloc/perf profiling；可信 executable 来自 retained authority。非空单一 `Py_SetPath` 在初始化前设置，配合 `getpath.py:258/:347/:465/:497/:546/:670` 的真实分支关闭 pyvenv、`._pth`、安装前缀和外部搜索项探测。不能将空字符串当同样的阻断条件。

更关键的是 frozen core `_bootstrap.py:1565–1570` 先 import `_frozen_importlib_external`，再调用其 `_install`。该名字已被 native built-in adapter 注册；`localcat_support_init:554–557` 在执行任何 external 源码之前清空实际 `sys.path`。失败就撤销，不恢复临时路径。E9 初始化后的 `localcat_isolation_reprove` 只是再检查不变量。

### nt 文件状态、SHELL32 与扩展导入

保留的 `_bootstrap_external.py:25–36` 只导入 built-in 依赖；导入 `nt`/`winreg` 不调用 `nt.stat`、startfile 或反射方法。`_install:1557–1562` 构造 loader 类清单并安装 hook，不执行 `ExtensionFileLoader.create_module`。

实际 `PathFinder._get_spec:1261` 仅遍历传入路径；顶层 `sys.path=[]`，encodings 包的 `__path__=()` 由 native `:589–591` 设置。于是不会进入 `_path_importer_cache` 的空项 CWD 转换、FileFinder 的 `_path_stat`，也不会从路径发现一个 PYD。编码源码由五个 native adapter 逐项从 retained handle 读取和编译，built-in/frozen 的已固定依赖不是磁盘 fallback。

这并未删除 `_imp.create_dynamic`。直接以人为构造的 ModuleSpec 调用它能够越过 PathFinder；正确切断理由是**这份实际执行源码中没有该调用**。retained external 的 `:1054`（及非 Windows 的 AppleFramework 分支 `:1487`）只在其方法真正被调用时进入；正常初始化与 `_install` 不调用它们。由 `import.c:_imp_create_dynamic_impl → importdl.c:_PyImport_GetModInitFunc → dynload_win.c:_PyImport_FindSharedFuncptrWindows` 才会到 python3/PYD 的四处 loader。因此不能把 `safe_path` 或空路径误写成通用 Python sandbox。

E11 的固定 bootstrap 只导入 `_localcat_frozen_bootstrap` 与 built-in `_thread`，`TrustedSourceLoader.execute:131–148` 直接编译 authority 返回的完整字节并 exec；critical 源码只比较 fixture 字节。它们不调用 nt 的路径函数、startfile 或 `_imp.create_dynamic`，不调用 SourceFileLoader、FileFinder 或自建 ExtensionFileLoader。原源码改动会改变 payload/pre-link/PE/release；未重签的追加和同长度篡改在执行前被拒绝。该反例证明输入边界，不等于已直接执行扩展导入反例。

## 5. 失败、traceback、worker 与退出

| 路径 | 必须保留的判断 |
|---|---|
| 清空路径前失败 | external importer 尚未安装；不能按随后“空路径”倒推更早行为。实际 borrowed-NULL 反例负责确认清空失败立即终止，未进入 handoff。 |
| retained 源码编译／执行／安装失败 | `support_init` 不恢复路径、不切换 frozen/disk fallback；`import.c:4240–4247` 自身会打印异常。retained 编译名为 `<localcat-retained:...>`；`traceback.c:506` 的尖括号条件在尝试打开源码之前返回。不是宣称所有 traceback 都不会访问文件。 |
| Python traceback formatter 导入 | 早期未安装 external importer 或随后空路径阻断外部 traceback/linecache 发现；保留源码及其 built-in/frozen 依赖才是允许输入。C fallback 与早期 `lost sys.stderr` 均不能被当作继续启动。实际异常帧、完整 stderr、撤销及未到 E10 由 retained-failure profile 核验。 |
| E11 未捕获错误 | `localcat_bootstrap_execute:724–737` 清错、撤销并返回原生失败 marker，不调用 Python 异常打印器；不能因 bootstrap 的 co_filename 是相对路径而笼统推断这里会重开源码。其他新增打印路径须重审。 |
| worker | exact bootstrap 的 `_probe_worker` 仅执行原生 read/reproof/close 反例，捕获 BaseException 并在 finally 释放锁；主线程核验 worker 身份与完成结果。线程建立／提前失败仍可能到 CPython 的 unraisable/fatal 分支，须受相同空路径与源码集合约束；本轮正常 worker 成功不是所有失败支线的实测覆盖。mimalloc FLS 回调和 `_thread` 固定系统函数查询不能忽略。 |
| CRT 正常／失败退出 | 正常完成和 E1 未成功时的返回均可进入 `exit → common_exit → AppPolicyGetProcessTerminationMethod`；仅固定 AppModel API-set 的 `0x800` 调用。若 mimalloc stats/verbose 开启，失败清理也可能走 PSAPI；不得因正常 profile 未命中就删掉。 |
| fatal/OOM | Py_SetPath 的内部 fatal 不返回普通初始化错误；realloc/retained allocation 等受控故障分别验证其真实调用方。本文没有穷尽所有 allocator、异常打印和 CRT fatal 分支。 |

没有调用 `Py_Finalize*` 的最小 native 路线不能被描述成“运行了全部 Python atexit”；同样，进程退出仍会执行 Windows DLL/CRT 的终止路径，不能据此省略 mimalloc／CRT 根。

## 6. 本轮结论与剩余验收责任

现有证据已把直接 loader IAT 引用、VCRuntime 导出跳板、所有直接 GetProcAddress 引用、mimalloc CRT initializer 和实际保留源码的主要调用链逐项对应。不是仅按字符串搜索源文件，也不是把 breakpoint no-hit 当不可达。

完整线性反汇编不是通用 CFG 证明器；本单元按既有 W3 要求使用固定输入的源码和机器码审查，而不是另设独立机械证明或官方 runtime 重建门槛。E8 函数槽、CRT 固定 resolver、initializer 表、built-in 导入／方法和 FLS 回调分别有上述具体根；`nt.stat`、SHELL32、python3/PYD 的切断依赖真实初始化和保留源码，不能用 no-hit 取代。

§3 的 **pre-E1 localization 分支漏记**已按 windows-platform-enablement 的 W3 流程获得用户批准最小补录，plan §5.4、candidate contract 与严格消费者同步声明其阶段、flags、固定符号及兼容后备。本文引用的旧产物仍是发现该遗漏的证据，不事后追授旧成品 PASS。修订候选的 materialize、双 clean build、实际解析与后备轨迹重绑，以及最终组合裁决另见[验收记录](spike-validation.md)；本调用点记录和一次真实返回 System32 KERNELBASE 均不单独授予 PASS。

pre-entry 解析、系统返回 host、flags `0` 兼容回退及 `.local`/SxS、alias/reparse 等应用可控重定向仍由独立系统入口证据负责；失败、污染和 retained-window 观察各自只覆盖其 profile，不代替未执行支线。新增源码、扩展模块或应用输入会改变这份调用闭包，必须重新核对。以上不要求审计 Windows 系统服务内部，也不要求新增 locale／企业部署条件。

## 附：关键原始源码摘要

所有路径相对相应源码根；完整大小与其余参照文件见本轮 `evidence.json.sources`。CPython archive 使用 LF，随包 stdlib 使用 CRLF：重放只为源码对应比较换行归一化，**执行授权始终比较随包原始字节**，`retained_stdlib` 同时记录两者关系。

| 源码 | 原始 SHA-256 |
|---|---|
| CPython `Include/internal/pycore_fileutils_windows.h` | `2ce33652ff0a955037aeacfcfa771b6acdcec0fe08d30c2a0ba07c82a6bb11f1` |
| CPython `Modules/posixmodule.c` | `436636d1ef1693d58e07cf80fc4aea58ad740e5d2e6b2f466164f66be5f8d43d` |
| CPython `Python/dynload_win.c` | `3e035909934e6349136b1419bdf78143719cd27de066779363300193dfde39c2` |
| CPython `Objects/mimalloc/init.c` | `9f0908c7c860958d00812e841a41387e20d2906c2f85621564edb63ee686e2ab` |
| CPython `Objects/mimalloc/prim/windows/prim.c` | `0d615b919136e6fe52140e42e4dac846fe71db087a827feb9e7d0b616a9176b9` |
| CPython `Modules/getpath.py` | `45097a751b0e59318ef55047d51e453c74bc851c5360b0d5104e6bb07ce44c5d` |
| CPython `Lib/importlib/_bootstrap.py` | `e50d35b1191ecbc5f7faecc73f54efd242bcf24c510e4ea5eb64bfba9458c0e3` |
| CPython `Lib/importlib/_bootstrap_external.py` | `b9af1fb7b625030cf5ee06b39aeb7838c2a30c97ddee09410286b7afbd8b7098` |
| 随包 `importlib/_bootstrap_external.py` | `3562b2fe92d20c72c29d21614c354703989974cb9cd867b57c56d2f0484c0dd9` |
| CPython `Python/traceback.c` | `29950ba81935754f532b773f254f745a2e269fe88d99bf48432580357443b324` |
| MSVC `winapi_downlevel.cpp` | `dde4c80ae0ed774f5aa4e45985c01688515db421cac8de4c49ad05df9a86275e` |
| SDK UCRT `internal/winapi_thunks.cpp` | `64d965d1989f1a5f8fe82932b5f90df78d29470dba287089ef5677ce9452df66` |
| SDK UCRT `mbstring/mbctype.cpp` | `1905612413f893388c1bbeec47df7eb51099384d00085c0a648203a4acd4443a` |
| SDK UCRT `initializers/multibyte_initializer.cpp` | `6943be2cb31c3380b882e071a4a0040eb0fdd6a339271563f775f458fd791f3a` |
| SDK UCRT `startup/exit.cpp` | `dacd99b1435235c285c591c22e4e28d731a8332996b2f513bcf9dc8b0fd3808e` |
| LocalCAT `native/localcat_frozen_bootstrap.c` | `d5fb72db1e1a1132814bc7e4555820379e2367cb16a10f7f096b7ac22fee8387` |
| LocalCAT `native/localcat_native_closure.c` | `66dab89e4201ed2b9890894c75a6db54916bc3d95be887d09ed2f17fd1ac604d` |
| LocalCAT `spike/localcat_frozen_bootstrap.py` | `6dacd5974c5c82caccdf5de2b12c4d8c2e5c728b2ae8a619bd2af0f289a64e47` |
| LocalCAT `spike/localcat_spike_critical.py` | `52fd66752a33af607ec480e9271dd5bedacfcafcbb902168cbbc6acfc12f9b03` |
