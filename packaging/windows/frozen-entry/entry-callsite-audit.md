# W3 入口调用点审查记录

本文记录会改变 E0.5/E4 验证方法的事实，不授予新的系统调用 allowlist，也不表示 Task 1.6 完成。对象是当前锁定 CPython 3.14.7 和五补丁产生的真实 windowed 封装入口，不是控制台替身。

## CRT：策略前存在明确的系统探测

EXE 的静态 CRT 在 `wWinMain` 设置搜索策略前探测三个固定 API-set：`api-ms-win-core-synch-l1-2-0`、`api-ms-win-core-fibers-l1-1-1`、`api-ms-win-core-fibers-l1-1-2`。前两条来自 VCRuntime 的 lock/FLS 初始化，后一条来自 UCRT 的 `FlsGetValue2` 探测。它们与 Windows 在 EXE entry 之前初始化系统 DLL 的调用应分开记录。

锁定 MSVC 的 `crt/src/vcruntime/winapi_downlevel.cpp` 与 SDK UCRT 的 `internal/winapi_thunks.cpp` 均先调用 `LoadLibraryExW(name, NULL, 0x800)`。实际封装 EXE 的调用指令也分别设置该参数。两份实现保留旧系统兼容回退：加载失败且错误为 `ERROR_INVALID_PARAMETER` 时，非 API-set 名称可能使用 flags=0；第一份排除 `api-ms-`，第二份同时排除 `ext-ms-`。因此上述三个 API-set 自身不能走 flags=0，但固定列表的真实 DLL 后备项仍需审计；这三条路径对应的后备目标为 `kernel32/kernelbase`。

不能将“没有新 ModLoad”写成“没有调用 loader”，也不能把共用 thunk 的全部名字都当作初始化前可达。仍需目标 profile 的 API-set host、KnownDLL/既有系统模块身份与应用可控重定向反例，才能证明默认搜索回退不引入 ambient 路径。

### 返回型失败也会执行 CRT 退出探测

锁定 EXE 的 `wWinMain` 返回后，无论返回码都经 `exit → common_exit → __acrt_get_process_end_policy → AppPolicyGetProcessTerminationMethod`。UCRT 的固定目标只有 `api-ms-win-appmodel-runtime-l1-1-2`，使用 `LoadLibraryExW(..., 0x800)`；非 secure process、函数与模块尚未缓存时才实际加载。该 API-set 不会进入共用 thunk 的 flags=0 回退，也没有真实 DLL 后备目标；查询失败保留默认 `ExitProcess` 策略。

正常完成之后的观测不能将此调用排除在 E10 前闭包之外：同一封装 EXE 在 launcher 环境拒绝以及带分号根路径的初始化配置拒绝后，均先输出失败 marker，再执行上述调用并以 1 退出。源码与机器码还表明 E1 策略 API 自身失败返回时会经过同一 CRT 退出链；不能把阶段统一写成“依赖 E1 成功”。修订合同纳入这个固定根，但其声明仍不能替代策略尚未建立时的系统目标解析证明。

原始自然拒绝反例和源码对应保存在 `artifacts/windows/early-exit-investigation-20260918/`。修订候选的 `artifacts/windows/crt-exit-observation-20260918/` 还显式模拟了 E1 API 返回 FALSE 和初始化 API 返回 −1：调试器跳过对应函数体后，真实调用方均产生预期诊断、经过 AppPolicy 探测并以 1 退出，磁盘输入前后未变。前者没有建立 E1 policy；后者只验证调用方清理，不是 Python 初始化内部真实失败、fatal 或 OOM。系统解析与重定向反例仍未由这些观测证明。邻近的 `mscoree.dll` 分支只查询已经加载的模块，不是主动加载；共享 thunk 中的 locale 调用也不因本次退出根审计自动获得授权。

## Python DLL：用户 DllMain 不等于整个加载闭包

官方源码的 `PC/dl_nt.c` 用户 DllMain 只记录实例句柄，但 `Objects/obmalloc.c` 静态包含 mimalloc；`Objects/mimalloc/init.c` 将初始化函数放在 MSVC `.CRT$XIU`。当前 DLL 的 `_initterm_e` 表包含这个函数。TLS 数据目录存在，但 loader TLS callback 表首项为空；线程退出使用的 FLS 注册不能混写成加载期 TLS callback。

| 调用 | 源码根与条件 | 验证影响 |
|---|---|---|
| `kernelbase.dll`、`ntdll.dll`、`kernel32.dll` | `mi_process_load → mi_process_init → _mi_os_init → _mi_prim_mem_init` 的三个 `LoadLibraryA` | 发生在 Python DLL CRT 初始化中，早于 E9，但晚于 E1；需纳入应用系统入口审计 |
| `bcrypt.dll` | `_mi_prim_random_buf` 在缓存函数指针为空时加载；CRT 的 `_mi_random_reinit_if_weak` 是一条带 weak 条件的到达路径，`_mi_random_init` 的直接请求不要求 weak 状态 | 是应用侧随机源调用分支，不是 CNG 内部实现；一次未命中不能删除该分支 |
| `psapi.dll` | 统计输出的 `mi_process_done → mi_stats_print → mi_process_info` | 默认初始化不是必经；`MIMALLOC_SHOW_STATS` 可使退出路径实际到达 |

mimalloc 通过 Win32 环境 API 读取选项，早于 Python 的 `use_environment=0`；不能用 Python 的隔离开关证明统计支线不存在。正常与 stats profile 实测均在主随机状态检查处读到 weak=0；本轮未命中 mimalloc bcrypt helper，只解释该次执行，不证明其不可达。stats profile 在完成 marker 之后、进程退出之前命中 PSAPI loader。

剩余源码分支还包括文件状态探测的 API-set loader、`os.startfile` 的 SHELL32 loader，以及扩展导入前的 `python3.dll` 探测和 PYD 加载。后者使用的 `0x1100` 不等于自定义 dispatcher 的 `0x900`；必须结合实际 retained importlib/source 闭包证明 E10 前不可达，不能只看初始化选项或导入表。

## 证据使用与合同缺口

可用 `trace_windows_frozen_entry` 对同一封装诊断目录重放 normal/stats profile。观察点偏移先绑定 exact Python DLL 摘要；实际执行仍需由当前原始日志、输入摘要与静态源/二进制关系共同复核。首版未限定模块的延迟断点产生告警；该轮汇总不能充当完整轨迹，新版明确拒绝此类结果。

发现时，`candidate-contract.json` 的调用清单只列自定义 dispatcher，mandatory 表的“无 policy 前 dynamic load”也未表达 CRT 的固定系统探测。修订后的 W3 计划明确列出这些调用与禁止非授权路径的条件；输入声明本身不证明实际解析、失败路径或闭包完整，E0.5/E4 仍须由最终候选反例和源/二进制证据裁决。
