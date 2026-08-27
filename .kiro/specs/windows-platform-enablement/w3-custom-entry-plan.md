# W3 custom in-process entry reapproval plan

- **状态**：DRAFT — awaiting W3 reapproval
- **Task**：`windows-platform-enablement` 1.5
- **上游事实**：Task 1.4 已证明 stock PyInstaller 6.22.2 `runw.exe` 六项 mandatory 断言全部失败
- **实施授权**：本文获批只允许准备 Task 1.6 最小 spike；不授权 Task 7、frozen WA merge 或发行

## 1. 路线裁决

首选路线是对已锁定 PyInstaller 6.22.2 sdist 的 Windows `runw` bootloader 做 release-owned、可重放的下游 patch，并仍由 PyInstaller 生成 `--onedir --windowed` candidate。patch 必须在**同一 GUI 进程的真实 PE entry**内建立 DLL policy、bundle rooted authority、native closure proof和native→Python handoff；另起父 wrapper、source launcher或对stock child事后注入hook均不属于此路线。

这不是信任任意“自定义 bootloader”。production build只接受获批upstream sdist digest、tracked patch/新增source、clean toolchain lock和重建出的bootloader digest。若patch无法在Python首次加载前满足本计划，Task 1.6返回W3；切换到独立launcher须重新提交entry/ABI/TCB delta，不得作为现场fallback。

## 2. 真实 entry 顺序

| 顺序 | 首次允许发生的动作 | 硬性结果 |
|---|---|---|
| E0 | Windows loader解析PE static imports | 仅允许版本化system allowlist；custom `runw`移除stock `COMCTL32!LoadIconMetric`依赖，windowed错误使用已批准的`USER32`诊断路径 |
| E0.5 | MSVC PE/CRT entry进入`wWinMain`前的compiled-in startup | static CRT与compiler-generated initializer属于content-addressed native TCB；必须用静态分析和startup trace证明其不加载非allowlisted module、不读取ambient DLL路径、不执行第三方initializer |
| E1 | `wWinMain`第一段LocalCAT patch代码 | 调用`SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32)`；不得先解析CWD、`PATH`、application data目录或加载非system DLL |
| E2 | 定位EXE与onedir root | 逐组件打开并保留root/intermediate handles；执行W1 rooted、reparse、volume、final path和live FileId不变量 |
| E3 | 读取pre-link input/native runtime manifest | 从retained handle读取固定格式runtime manifest；校验embedded input-manifest/schema/root digest，拒绝extra、duplicate basename和未声明dynamic root |
| E4 | 预证明native closure并关闭dynamic call surface | 对Python DLL及其递归static/delay依赖逐项retained open、digest和identity；同时静态审计custom entry、CRT、每个非system binary及初始化前可达代码中的`LoadLibrary*`/`LdrLoadDll`/delay-load/extension-import callsite。除受审dispatcher及批准System32调用外不得存在初始化前可达dynamic loader；`DllMain`也在此约束内，声明或预加载本身不能代替该证明 |
| E5 | 预证明source/fixture closure | 在Python初始化前逐组件rooted open并保留bootstrap、解释器启动所需exact-byte代码、一个critical `.py`和一个fixture的handle，记录identity/digest；同时拒绝同名PYZ/`.pyc`/`__pycache__`、extra source及manifest dependency cycle |
| E6 | 受限加载 | 每个非system DLL只能由E4受审dispatcher使用已证明绝对路径和`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32`加载；不把多个`AddDllDirectory`目录的未规定顺序作为authority |
| E7 | actual-module reproof | `GetModuleFileNameW(HMODULE)`取得实际pathname，在仍由no-write/no-delete root/source handles固定的同一parent下rooted reopen，再比较live FileId/final path与E4 retained handle；basename唯一性已由E3固定。任何`DllMain`后才发现的不一致仍判失败，不倒推出pre-load PASS |
| E8 | 绑定Python C API并注册built-in | `python314.dll`不作为PE静态import；以`GetProcAddress`绑定获批的exact symbol table，在解释器初始化前调用`PyImport_AppendInittab("_localcat_w3_native", ...)`；任何缺失/额外symbol或注册失败均在此停止 |
| E9 | 初始化isolated Python | 以`PyConfig_InitIsolatedConfig`、显式manifest-bound module paths和`Py_InitializeFromConfig`初始化；`use_environment=0`、`user_site_directory=0`、`site_import=0`、`safe_path=1`、`parse_argv=0`且不含checkout/CWD；该阶段不得到达E4未批准的native loader callsite |
| E10 | 建立不可伪造handoff | import已在E8注册的compiled-in `_localcat_w3_native`，mint并take一个one-shot opaque extension authority；其内容绑定E4/E5/E7证明且不向Python返回raw HANDLE |
| E11 | 执行可信bootstrap与source-only module | bootstrap从E5 native retained handle读取并直接编译；随后`TrustedSourceLoader`以同一authority执行critical `.py`和读取fixture，逐次复核live identity/digest并保持duplicate拒绝 |

E0属于process-entry前外部证明；E0.5必须证明compiler/CRT startup没有在policy前打开新的load surface；E1～E11由native trace与post-load inventory证明。任何Python runtime hook都晚于E9，不能代答E0～E8。`LdrRegisterDllNotification`最多作为trace/fail-fast telemetry：callback不能否决已开始的load，因此不得充当E4 enforcement。

## 3. Runtime manifest与TCB

build采用无自引用的两阶段manifest。第一阶段canonical **pre-link input manifest**内容寻址upstream/custom source、patch、toolchain、runtime/source/fixture entries与依赖，但明确排除尚未生成的resulting PE；它派生固定布局native runtime manifest，其digest/schema/root digest编译进executable。第二阶段**post-build release manifest**绑定resulting PE SHA-256、pre-link digest、derived runtime-manifest digest及完整dist inventory。两者均由clean build生成，不是第二份手写authority。native parser只接受定长header、checked offsets/counts、UTF-8 relative names、SHA-256和显式role/dependency索引；整数溢出、重叠range、非法UTF-8/路径、重复entry/basename、dependency cycle、未知flag或trailing bytes均fail closed。

每个candidate根据pre-link input digest生成header并重编custom bootloader；不能在通用`runw.exe`复制完成后现场写入一个未绑定的digest。PyInstaller后续append CArchive、写resource/manifest的步骤及其输出EXE只进入post-build release manifest。native hash实现首版固定为compiled-in SHA-256并纳入source/compiler TCB，不从application directory或未固定provider加载hash helper。

最小spike的Boot TCB仅包含：

- customized `runw.exe`及其tracked upstream source/patch、新增native rooted/hash/manifest/handoff代码；
- embedded runtime-manifest schema/root digest；
- `python314.dll`和解释器初始化前真实加载的全部非system native closure；
- `_localcat_w3_native` built-in module与最小Python C API symbol table；
- exact-byte bootstrap、其解释器启动所需PyInstaller bootstrap/stdlib集合，以及这些代码的动态native roots；
- 一个source-only critical module、一个fixture和对应manifest entries；
- 版本化Windows system DLL/API-set allowlist与E0 external resolution evidence。

`ctypes`不进入最小bootstrap路径；若实际PyInstaller/Python启动在authority建立前需要它或其他extension，必须显式加入TCB和dynamic-root closure，而不是依赖运行时偶然导入。

## 4. Native→Python ABI

ABI名为`localcat.w3.bootstrap-attestation.v1`，由compiled-in module单次mint并单次take为不可构造、不可序列化的opaque extension type `W3Authority`，不使用裸`PyCapsule`作为Python调用协议。native对象内部至少绑定：ABI version/size、bootloader build id、pre-link input manifest digest、bundle root identity、每个E4/E5 retained entry的role/FileId/digest、actual-module reproof结果和native trace digest。状态机固定为`CREATED → TAKEN → CLOSED`；仅创建它的主解释器及初始化线程可take，take后所有权转移给`TrustedSourceAuthority`，析构或显式close统一关闭handles。Python API只允许：

- `_localcat_w3_native.take_attestation()`：仅成功一次并返回`W3Authority`；重复、提前、错误线程/解释器或handoff缺失均失败；
- `authority.read_verified(entry_id)`：从既有retained handle读exact bytes并复核live identity/digest；
- `authority.module_reproof(entry_id)`：返回native层对实际loaded module完成的只读证明；
- `authority.close()`：统一关闭authority；close/析构后所有操作及已取得的loader引用都失败，不能按pathname恢复。

type名、指针cookie或Python对象identity都不是独立信任根；可信性来自E0～E10无未批准代码执行的顺序、compiled-in producer和native retained handles。bootstrap不得接受pickle/JSON/environment/argv传入的等价对象，也不得自行按pathname reopen。

## 5. Toolchain input profile

选择MSVC x64静态bootloader路线，不使用MinGW/Cygwin。PyInstaller官方文档支持从sdist用Visual C++重建bootloader；MSVC路线可生成self-contained static executable，减少bootloader自身CRT DLL pre-entry闭包。

| 输入 | Task 1.5选择 | 进入1.6前的pin要求 |
|---|---|---|
| CPython | CPython 3.14.7 x64；`python314.dll` SHA-256 `0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700` | Python安装/embedded输入、DLL、import library与headers全部摘要 |
| PyInstaller | 6.22.2 official sdist SHA-256 `89b65a3ad07d9dd5832253e37bc45f31872d10d7f9d5c9fd0fdd6088a83829dd`；audited source aggregate `e9815b1301b5aaa706b44b55ee49c2348511b8d8c92f1dcc86a2b41602cb4826` | sdist、51-file bootloader/loader aggregate和applied patch digest一致 |
| Compiler | Visual Studio 2022 Build Tools fixed release `17.14.39`（build `17.14.37614.0`），MSVC x64 component，release build，static CRT，`/guard:cf`、`/Brepro`、无debug directory | fixed bootstrapper/layout catalog、实际MSVC component/toolset version、`cl.exe`/`link.exe`及实际include/lib输入摘要写入toolchain lock |
| Windows SDK | Windows 11 SDK `10.0.26100` API surface；首选已由VS 17.14 servicing提供的`10.0.26100.4188`包，只使用本计划列出的Win32 API | SDK package/catalog identity、headers/libs/`mt.exe`/`rc.exe`实际输入摘要写入lock；resolved package若不是所选版本先回到W3 |
| Build driver | sdist内Waf及tracked build wrapper | Python executable、Waf source、arguments、environment projection与stdout/stderr入证据 |
| Custom source | upstream source + repository tracked patch/new files | clean commit、逐文件摘要、patch apply结果、compiler/linker flags与resulting PE摘要 |

当前host未安装compiler或Windows SDK，因此所选fixed release的bootstrapper/layout、resolved component版本与逐文件摘要尚未materialize。本文只能先确认候选路线和pin schema；**Task 1.5保持未完成且不得批准**，直至同一W3 review拿到materialized toolchain lock、applied patch digest、exact Python C API symbol table、完整pre-authority TCB/native dependency inventory和下述system allowlist。1.6不能接收“开始后再补”的lock。

### 5.1 PE/system allowlist boundary

proposed custom `runw.exe`自身E0 static import只允许`KERNEL32.DLL`、`ADVAPI32.DLL`、`GDI32.DLL`、`USER32.DLL`；`COMCTL32.DLL`必须消除。API-set名称只在materialized PE inventory逐项列出并证明解析到获批System32/KnownDLL host后才允许。Python DLL、其非system依赖和每个system/API-set传递成员必须从实际locked binary递归生成exact allowlist；compiler生成任何额外import、delay import或manifest dependency即阻断1.5，而不是扩张通配allowlist。当前host的KnownDLLs观察仅是scout输入，不替代目标profile上的external resolution proof。

pre-authority动态加载首版采用“**可达调用点闭包**”而非OS级全局拦截：custom dispatcher是唯一获准的非system loader callsite；CRT、Python DLL `DllMain`、CPython初始化到E10之间的可达源码/反汇编与startup trace必须证明不会调用其他`LoadLibrary*`/`LdrLoadDll`/delay helper或native extension import。无法证明的binary/callsite直接NO-GO；E7 post-load inventory或loader notification不能追认。完整Task 7若Qt/PySide6/SQLite引入新的pre-authority callsite，须扩展同一闭包并重回W3。

## 6. Mandatory spike assertion matrix

| ID | 刺激/观察 | 通过条件 |
|---|---|---|
| `W3.CUSTOM.PE_SYSTEM_ONLY` | 静态/delay import、MSVC CRT startup与启动module inventory | E0/E0.5仅出现allowlisted system/API-set identity；无`COMCTL32`歧义、第三方initializer或policy前dynamic load |
| `W3.CUSTOM.DLL_POLICY_FIRST` | CWD/PATH/application-dir放置同名Python/CRT probe DLL | 首次非system load前policy已生效，probe不执行 |
| `W3.CUSTOM.BUNDLE_ROOTED` | bundle/ancestor junction、final reparse、ancestor swap | 全部在load前稳定fail closed，无path-only fallback |
| `W3.CUSTOM.NATIVE_CLOSURE_PRELOAD` | 顶层及传递DLL替换、missing/extra、duplicate basename | 每个非system member在首次load前已有retained identity+digest proof |
| `W3.CUSTOM.DYNAMIC_ROOTS_DECLARED` | pre-authority hook/extension新增未声明`LoadLibrary*` root | build或startup失败；不得在post-load inventory中追认 |
| `W3.CUSTOM.ACTUAL_MODULE_REPROOF` | proof后到load间swap与loaded-module path/FileId复核 | 实际module逐项等于pre-load retained identity；不一致失败 |
| `W3.CUSTOM.HANDOFF_ONE_SHOT` | 缺失、重复take、错误线程/解释器、伪造对象、跨进程/序列化重放、close后复用 | 只有同进程compiled-in producer的单次opaque authority可用，状态严格`CREATED→TAKEN→CLOSED` |
| `W3.CUSTOM.MANIFEST_EXACT` | manifest digest、offset/count/overlap/overflow、非法UTF-8/路径、duplicate entry/basename、dependency cycle、unknown flag、extra/trailing bytes tamper | parser、embedded pre-link root绑定及post-build release binding全部fail closed |
| `W3.CUSTOM.SOURCE_EXACT_BYTES` | critical source proof后swap/tamper | executed digest等于retained-handle digest和manifest；swap不能改变bytes |
| `W3.CUSTOM.SOURCE_ONLY` | 同名PYZ/`.pyc`/`__pycache__` duplicate | build与runtime均拒绝；执行loader仅为`TrustedSourceLoader` |
| `W3.CUSTOM.FIXTURE_EXACT_BYTES` | fixture missing/tamper/swap | 只从retained handle读取，digest/identity错误失败 |
| `W3.CUSTOM.NONREPO_CWD` | 无checkout、净化`PATH/PYTHONPATH/Qt`、非仓库CWD | spike正常PASS且无checkout/CWD read |
| `W3.CUSTOM.FAILURE_DIAGNOSTIC` | windowed pre-Python、bootstrap、loader各阶段失败 | 产生稳定无正文诊断marker，进程非零退出，不静默成功 |
| `W3.CUSTOM.REPRODUCIBLE_INPUTS` | 两个clean build重放toolchain lock | input/runtime/release manifest与patch一致；规范化只允许清零COFF `TimeDateStamp`和OptionalHeader `CheckSum`，要求无Debug/Security Directory，剩余PE/dist bytes完全一致，否则失败 |

1.6只有上述全部mandatory断言在同一custom entry build上PASS才完成。普通PyInstaller build成功、Qt窗口出现、source lane通过或单一positive smoke均不能替代此矩阵。

## 7. 维护与升级边界

- PyInstaller、CPython minor/patch ABI、compiler/SDK、bootloader patch、PE imports、Python C API表、manifest schema、system allowlist、pre-authority module/dynamic roots任一变化，都重建TCB并至少重跑1.6矩阵。
- 只变Windows PowerShell 5.1 Build/Revision不改变W3 TCB；它是evidence orchestrator事实，由`CLEAN_WINDOWS_V2`记录而不阻断。PowerShell edition/major/minor/architecture、system-host选择或行为profile变化仍阻断证据lane。
- Task 1.6只裁决最小机制可行性；完整Qt/PySide6/SQLite/native closure、owner roots、resources和全业务E2E仍归Task 7～10，不得由最小spike冒充。
- upstream若接受等价实现，仍须按新source/digest重跑；不得仅因“已upstream”降低release-owned review和attack matrix。
