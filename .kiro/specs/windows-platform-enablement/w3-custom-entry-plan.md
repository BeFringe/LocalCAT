# W3 custom in-process entry reapproval plan

- **状态**：APPROVED — authorizes Task 1.6 minimal spike preparation only
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
| E4 | 预证明应用 native closure 并约束 dynamic call surface | 对 Python DLL 及其递归非系统 static/delay 依赖逐项 retained open、digest 和 identity；静态审计 custom entry、CRT、每个非系统 binary 及初始化前可达代码中的 `LoadLibrary*`/`LdrLoadDll`/delay-load/extension-import callsite（含 `DllMain`）。只允许受审 dispatcher 与获批系统 API 入口，系统入口边界见 §5.2 |
| E5 | 预证明source/fixture closure | 在Python初始化前逐组件rooted open并保留bootstrap、解释器启动所需exact-byte代码、一个critical `.py`和一个fixture的handle，记录identity/digest；同时拒绝同名PYZ/`.pyc`/`__pycache__`、extra source及manifest dependency cycle |
| E6 | 受限加载 | 每个非system DLL只能由E4受审dispatcher使用已证明绝对路径和`LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_SYSTEM32`加载；不把多个`AddDllDirectory`目录的未规定顺序作为authority |
| E7 | actual-module reproof | `GetModuleFileNameW(HMODULE)`取得实际pathname，在仍由no-write/no-delete root/source handles固定的同一parent下rooted reopen，再比较live FileId/final path与E4 retained handle；basename唯一性已由E3固定。任何`DllMain`后才发现的不一致仍判失败，不倒推出pre-load PASS |
| E8 | 绑定Python C API | `python314.dll`不作为PE静态import；以`GetProcAddress`绑定获批的exact symbol table并固定built-in init function；任何缺失/额外symbol均在此停止 |
| E9 | 初始化isolated Python并注册built-in | CPython 3.14 保持 PEP 741 isolated 配置与 compiled-in handoff；按已批准的 `w3-e9-path-amendment.md`，仅新增兼容 ABI `Py_SetPath` 与 exact-byte `_bootstrap_external.py` interpreter entry，关闭 getpath 探测并在外部导入器安装前清空实际搜索路径。真实 EXE 来自 retained authority，launcher 环境覆盖在 Python DLL 加载前拒绝；配置、源码或安装失败均终止并撤销 authority，不得到达 E4 未批准的 native loader callsite |
| E10 | 建立不可伪造handoff | import已在E9通过init config注册的compiled-in `_localcat_frozen_bootstrap`，mint并take一个one-shot opaque extension authority；其内容绑定E4/E5/E7证明且不向Python返回raw HANDLE |
| E11 | 执行可信bootstrap与source-only module | bootstrap从E5 native retained handle读取并直接编译；随后`TrustedSourceLoader`以同一authority执行critical `.py`和读取fixture，逐次复核live identity/digest并保持duplicate拒绝 |

E0属于process-entry前外部证明；E0.5必须证明compiler/CRT startup没有在policy前打开新的load surface；E1～E11由native trace与post-load inventory证明。任何Python runtime hook都晚于E9，不能代答E0～E8。`LdrRegisterDllNotification`最多作为trace/fail-fast telemetry：callback不能否决已开始的load，因此不得充当E4 enforcement。

## 3. Runtime manifest与TCB

build采用无自引用的两阶段manifest。第一阶段canonical **pre-link input manifest**内容寻址upstream/custom source、patch、toolchain、runtime/source/fixture entries与依赖，但明确排除尚未生成的resulting PE；它派生固定布局native runtime manifest，其digest/schema/root digest编译进executable。第二阶段**post-build release manifest**绑定resulting PE SHA-256、pre-link digest、derived runtime-manifest digest及完整dist inventory。两者均由clean build生成，不是第二份手写authority。native parser只接受定长header、checked offsets/counts、UTF-8 relative names、SHA-256和显式role/dependency索引；整数溢出、重叠range、非法UTF-8/路径、重复entry/basename、dependency cycle、未知flag或trailing bytes均fail closed。

每个candidate根据pre-link input digest生成header并重编custom bootloader；不能在通用`runw.exe`复制完成后现场写入一个未绑定的digest。PyInstaller后续append CArchive、写resource/manifest的步骤及其输出EXE只进入post-build release manifest。native hash实现首版固定为compiled-in SHA-256并纳入source/compiler TCB，不从application directory或未固定provider加载hash helper。

最小spike的Boot TCB仅包含：

- customized `runw.exe`及其tracked upstream source/patch、新增native rooted/hash/manifest/handoff代码；
- embedded runtime-manifest schema/root digest；
- `python314.dll`和解释器初始化前真实加载的全部非system native closure；
- `_localcat_frozen_bootstrap` built-in module与最小Python C API symbol table；
- exact-byte bootstrap、其解释器启动所需PyInstaller bootstrap/stdlib集合，以及这些代码的动态native roots；
- 一个source-only critical module、一个fixture和对应manifest entries；
- 应用所用 Windows system DLL/API-set 入口 allowlist 与 E0 external resolution evidence。

`ctypes`不进入最小bootstrap路径；若实际PyInstaller/Python启动在authority建立前需要它或其他extension，必须显式加入TCB和dynamic-root closure，而不是依赖运行时偶然导入。

## 4. Native→Python ABI

ABI名为`localcat.frozen-bootstrap-attestation.v1`，由compiled-in module单次mint并单次take为不可构造、不可序列化的opaque extension type `FrozenBootstrapAuthority`，不使用裸`PyCapsule`作为Python调用协议。native对象内部至少绑定：ABI version/size、bootloader build id、pre-link input manifest digest、bundle root identity、每个E4/E5 retained entry的role/FileId/digest、actual-module reproof结果和native trace digest。状态机固定为`CREATED → TAKEN → CLOSED`；仅创建它的主解释器及初始化线程可take，take后所有权转移给`TrustedSourceAuthority`，析构或显式close统一关闭handles。Python API只允许：

- `_localcat_frozen_bootstrap.take_attestation()`：仅成功一次并返回`FrozenBootstrapAuthority`；重复、提前、错误线程/解释器或handoff缺失均失败；
- `authority.read_verified(entry_id)`：从既有retained handle读exact bytes并复核live identity/digest；
- `authority.module_reproof(entry_id)`：返回native层对实际loaded module完成的只读证明；
- `authority.close()`：统一关闭authority；close/析构后所有操作及已取得的loader引用都失败，不能按pathname恢复。

type名、指针cookie或Python对象identity都不是独立信任根；可信性来自E0～E10无未批准代码执行的顺序、compiled-in producer和native retained handles。bootstrap不得接受pickle/JSON/environment/argv传入的等价对象，也不得自行按pathname reopen。

### 4.1 完整产品的worker消费与生命周期

2026-09-19批准的WA-06 R4补齐Core 9.6c/9.6d及WA-07 3.6a消费，不改变opaque ABI与take次数。完整产品保持native read/reproof/close也在原owner thread/interpreter串行执行；平台内部调度向后台proof window交付受撤销约束的bytes/attestation。WA-07仅在Boot TCB闭合后注入Qt调度，平台和Core均不反向依赖Qt；headless worker在自己的初始线程履行owner合同。关闭/取消/异常/重入应撤销session并唤醒等待者，发布前必须fresh terminal reproof，不能永久信任启动缓存或排队旧证明。

每个benchmark child必须独立经过同一候选的E0～E10与自己的one-shot handoff。E10后由固定trusted bootstrap将两个允许的内部模式映射到migration/query worker，默认模式进入产品；未知/重复/多余模式与任意`-m`拒绝。模式只选择工作，禁止参数、父进程对象、JSON或environment传入authority，选择worker前不导入Core、platform factory、Qt或SQLite。

windowed request/result使用父进程创建、定向继承的pipes；E10后才接入Core严格codec。错误句柄、畸形/截断结果、非零退出与timeout遵守Core失败合同，fresh process/RSS范围不变；pipe不是authority，不回落venv或进程内worker。完整实现归平台7.2，Core pre-build tests不代替native/full runtime；本节不缩减§7维护触发器及Task 1.6完整mandatory重验。

## 5. Toolchain input profile

选择MSVC x64静态bootloader路线，不使用MinGW/Cygwin。PyInstaller官方文档支持从sdist用Visual C++重建bootloader；MSVC路线可生成self-contained static executable，减少bootloader自身CRT DLL pre-entry闭包。

| 输入 | 长期兼容线 | 当前及后续candidate进入1.6前的exact lock要求 |
|---|---|---|
| CPython | CPython 3.14.x x64；patch版本变化按维护边界重验 | 当前candidate固定3.14.7、`python314.dll` SHA-256 `0f9857ffdfe010fe6b99328d58c2e3c7472ce75f336bf9c2ad9bd5bca3bce700`，并摘要Python安装/embedded输入、DLL、import library与headers |
| PyInstaller | PyInstaller 6.22.x official sdist；patch版本变化按维护边界重验 | 当前candidate固定6.22.2 sdist SHA-256 `89b65a3ad07d9dd5832253e37bc45f31872d10d7f9d5c9fd0fdd6088a83829dd`、51-file source aggregate `e9815b1301b5aaa706b44b55ee49c2348511b8d8c92f1dcc86a2b41602cb4826`、patch owner file set及有序apply合同 |
| Compiler | candidate获批时受Microsoft支持、且与锁定PyInstaller source兼容的Visual Studio Build Tools MSVC x64 toolset；release build、static CRT、`/guard:cf`、`/Brepro`；PE Debug Directory固定为单一`IMAGE_DEBUG_TYPE_REPRO`，不生成CodeView/PDB | candidate构建前固定支持来源/核验日期、bootstrapper/offline layout或等价安装catalog、实际VS product/build、MSVC component/toolset、`cl.exe`/`link.exe`及实际include/lib输入摘要；每次clean build preflight重新摘要实际输入并与同一lock比较 |
| Windows SDK | candidate获批时受支持、与所选MSVC及目标Windows版本兼容且提供本计划所需Win32 API的Windows SDK | candidate构建前固定支持来源/核验日期、SDK package/catalog identity、实际版本及headers/libs/`mt.exe`/`rc.exe`输入摘要；每次clean build preflight重新摘要实际输入并与同一lock比较，SDK变化形成新candidate并回到W3重验 |
| Build driver | sdist内Waf及tracked build wrapper | producer Python source/native base runtime、独立venv语义与launcher、实际`pefile` source/version metadata、`vswhere.exe`、Waf source、arguments、净化environment projection与stdout/stderr入证据；wrapper复制无build cache的pristine source后、Waf运行前先摘要实际build-copy的完整bootloader build-driver输入，再按请求版本现场建立vcvars环境，核对`cl/link/rc/mt`解析路径并重建stock probe，只以exact import-table delta约束candidate，不把非`/Brepro` probe字节当发行authority |
| Custom source | upstream source + repository tracked patch/new files | 1.5固定owner file scope、patch series顺序、apply/冲突规则与编译链接合同；1.6记录clean commit、逐文件摘要、applied patch digest、compiler/linker flags与resulting PE摘要 |

每个candidate使用本机或CI上按上表在**构建前**materialize并由同一W3 review批准的实际工具链，以candidate-input lock唯一标识；工具链升级建立新lock和新candidate。Task 1.5在materialized compiler/SDK/tool摘要、upstream/runtime摘要、patch owner/apply合同、exact Python C API目标表、pre-authority TCB边界和下述expected PE/system allowlist齐备并获批后完成。Task 1.6在W1 rooted contract冻结后实现并应用完整patch，生成applied patch/source digest、resulting PE、realized C API绑定表、实际native dependency inventory与PE/system allowlist，并将它们写入realized-build lock；任一结果偏离1.5合同即回到W3。最终用户运行profile只包含发行runtime。

candidate-input lock只收录构建前已materialize的输入与获批目标合同，realized-build lock收录同一candidate的applied source和构建事实；两者通过candidate-input digest绑定，且realized API、import、dynamic-root与TCB边界必须是目标合同的精确实现。

### 5.1 PE/system allowlist boundary

custom `runw.exe`自身E0 expected static import只允许`KERNEL32.DLL`与`USER32.DLL`，后者仅保留`MessageBoxA`错误诊断；精确symbol集合由`candidate-contract.json`相对锁定stock表的显式增减派生。移除stock路径不再使用的imports；`KERNEL32!OutputDebugStringW`仅用于下述可选阶段观测。锁定Python DLL及其非system依赖递归生成runtime expected allowlist。1.6从resulting PE生成realized static/delay/manifest inventory，逐项证明API-set解析到获批System32/KnownDLL host；compiler产生任何额外import、delay import或manifest dependency均回到W3，而不是扩张通配allowlist。当前host的KnownDLLs观察仅是scout输入，不替代目标profile上的external resolution proof。

pre-authority 动态加载首版采用“**应用可达调用点闭包**”：custom dispatcher 是唯一获准的非系统 loader callsite；CRT、Python DLL `DllMain`、CPython 初始化到 E10 之间的可达源码/反汇编与 startup trace 必须证明应用代码不会到达 dispatcher 及获批系统 API 入口以外的 loader 或 native extension import。无法证明的应用 binary/callsite 直接 NO-GO，E7 inventory 或 notification 不能追认。Task 7 若 Qt/PySide6/SQLite 引入新的应用 pre-authority callsite，须扩展同一闭包并重回 W3。

### 5.2 初始化 RNG 与系统 API 入口

沿用 ADR-022 决策 5 的 KnownDLL/System32 信任与 ADR-023 决策 6 的非系统 native closure：应用声明并审计系统 API 入口和解析策略，系统服务实现由 Windows 提供。

- 锁定 CPython 在 E9 通过 `bcrypt!BCryptGenRandom(NULL, buffer, size, BCRYPT_USE_SYSTEM_PREFERRED_RNG)` 取得 hash 随机种子，flags 为 `2`，保持解释器正常随机化及错误处理。
- candidate-contract/input v3 绑定上述调用合同。系统观测保存在独立诊断产物中；构建输入绑定声明的应用闭包与系统入口策略。
- 系统入口与非系统 dispatcher 按各自阶段验证搜索策略及调用时序：§5.4 的 E0.5 CRT 探测使用 pre-entry system-resolution proof，CRT 退出探测使用不依赖 E1 成功的解析证明，其余要求 E1 策略的调用须证明策略已建立；均覆盖 CWD/PATH、`.local`/SxS、同名 DLL 和 reparse/alias 重定向反例。

v1 terminal NO-GO 工具只重放旧合同，拒绝 v2/v3 输入。诊断、parser 单测或初始化 probe 均不能单独把下列 mandatory candidate 断言标成 PASS。

### 5.3 最小绑定表与 windowed 阶段观测

custom entry 的 E8 目标为 37 个函数和 `PyExc_RuntimeError` 数据导出。目标合同显式列出从 stock PEP-741 表移除的函数及 custom 增量，不再将整个 stock 启动表并入目标；签名仍来自锁定源码声明和显式增量表。缺失、额外绑定或签名差异均拒绝，不能通过解析未使用函数凑齐旧表。

原生代码在易触发 fatal 的调用前先记录稳定阶段状态，再向可用的 stderr 和 `OutputDebugStringW` 尝试输出无正文标记。stderr、调试器或观察者均不是启动前置条件，输出不可用不得撤销有效 authority；可返回的真实失败仍通过错误诊断并非零退出。fatal/OOM 保持原生阶段记录与非零终止，在验收时由外部观察者取证；不承诺无人观察时的持久化日志。此修订须覆盖无 stderr 的真实 windowed entry 正常启动及失败路径。

### 5.4 已批准的 CRT/mimalloc 系统调用增量

E0.5 禁止的是未声明或可落入非授权路径的加载，不以“没有新增 module”代替调用点审计。锁定 CRT 在 E1 前的固定探测按下表进入精确目标合同；本表只声明可验证的调用边界，不预先授予解析成功或安全结论。

| 应用调用根 | 固定目标与搜索条件 | 阶段与必须证明的边界 |
|---|---|---|
| VCRuntime critical-section/FLS 初始化 | `api-ms-win-core-synch-l1-2-0 → kernel32`；`api-ms-win-core-fibers-l1-1-1 → kernel32` | E0.5，首试 `LoadLibraryExW(..., 0x800)`；仅首试失败且错误为 87、名称非 `api-ms-` 时，真实系统 DLL 后备项可能使用 flags=0；须有 pre-entry system-resolution proof |
| UCRT FLS2 初始化 | `api-ms-win-core-fibers-l1-1-2 → kernelbase` | 同上，回退还排除 `ext-ms-`；不得推广到共用 thunk 的全部名字或第三方 initializer |
| UCRT multibyte 大小写表初始化 | `LCMapStringEx`、后备转换 `LocaleNameToLCID` 使用 `api-ms-win-core-localization-l1-2-1 → kernel32`，首试 `LoadLibraryExW(..., 0x800)` | E0.5；ANSI 代码页非 UTF-8、GetCPInfo 成功且对应函数／模块缓存为空时可触发。须有 pre-entry system-resolution proof，不能以 Python UTF-8 模式或本机 ACP=65001 排除；不要求用户修改区域设置 |
| UCRT 进程终止策略查询 | `AppPolicyGetProcessTerminationMethod` 仅以 `LoadLibraryExW(..., 0x800)` 探测 `api-ms-win-appmodel-runtime-l1-1-2`，无后备目标或 flags=0 回退 | CRT 正常及失败退出，包含 E1 策略未建立时；进程非 secure 且函数与模块尚未缓存才加载。须独立证明系统目标解析，不以 E1 成功为前提 |
| Python DLL 内 mimalloc memory 初始化 | `kernelbase.dll`、`ntdll.dll`、`kernel32.dll` 的 `LoadLibraryA` | E6 的 DLL CRT 初始化，必须已建立 E1 System32 策略并证明系统目标解析 |
| mimalloc OS 随机源请求 | `bcrypt.dll` 的 `LoadLibraryA`，只在缓存函数指针为空时加载 | E1 后；CRT 的 weak-seed 重试是其中一条到达路径，不将该条件误写成所有随机源请求的前提；不改变解释器 hash RNG 合同 |
| mimalloc 统计输出 | `psapi.dll` 的 `LoadLibraryA`，统计/verbose 开启且缓存为空 | E1 后，包含 E10 之前失败退出的清理支线；Python 隔离开关不屏蔽 mimalloc 环境选项 |

所有目标、调用 API、阶段、条件和兼容回退逐项进入 `application_system_calls`；生成器和当前 candidate 消费者拒绝缺项、扩项或条件改变。非系统 dispatcher 及其 `0x900` 加载策略不变。仍须验证 API-set/KnownDLL/System32 解析以及 CWD/PATH、同名 DLL、`.local`/SxS、reparse/alias 反例；flags=0 回退不能仅凭名字或事后 inventory 放行。扩展导入、文件探测和 SHELL32 等未闭合的应用调用点不因这次增量自动获得授权。

CRT 退出探测的加载或符号查询失败保留既有 `ExitProcess` 默认策略，不新增应用 fallback。正常完成后的观测不能排除它在 E10 前失败路径的可达性；必须另验 E1 API 失败、E9 可返回失败及各自退出时序。API-set 在本机解析到的 host 是系统解析证据，不另加为应用显式加载目标。

UCRT multibyte 的候选循环仅在模块加载失败时前进；首个可用模块上符号缺失，不再继续遍历下一候选。LCMapStringEx 缺失转向静态导入的 LCMapStringW，并由 LocaleNameToLCID wrapper 转换 locale；该 wrapper 使用同一候选列表与模块缓存，符号仍缺失则使用 downlevel 转换，不引入额外 loader 目标。flags=0 兼容回退仅限前次 flags=0x800 失败且错误为87、目标名称非 `api-ms-`／`ext-ms-` 的真实 `kernel32` 候选；不向 localization API-set 开放 flags=0。本增量只补齐既有 CRT 分支的应用调用声明及验证，不扩大 Windows OS 信任范围。

## 6. Mandatory spike assertion matrix

| ID | 刺激/观察 | 通过条件 |
|---|---|---|
| `W3.CUSTOM.PE_SYSTEM_ONLY` | 静态/delay import、MSVC CRT startup与启动module inventory | E0/E0.5仅出现allowlisted system/API-set identity；无`COMCTL32`歧义、第三方initializer、未声明或可落入非授权路径的policy前dynamic load |
| `W3.CUSTOM.DLL_POLICY_FIRST` | CWD/PATH/application-dir放置同名Python/CRT probe DLL | 首次非system load前policy已生效，probe不执行 |
| `W3.CUSTOM.BUNDLE_ROOTED` | bundle/ancestor junction、final reparse、ancestor swap | 全部在load前稳定fail closed，无path-only fallback |
| `W3.CUSTOM.NATIVE_CLOSURE_PRELOAD` | 顶层及传递DLL替换、missing/extra、duplicate basename | 每个非system member在首次load前已有retained identity+digest proof |
| `W3.CUSTOM.DYNAMIC_ROOTS_DECLARED` | pre-authority hook/extension 新增未声明应用 native root；应用可控的系统入口路径重定向 | build 或首次相关调用前失败；应用调用点与系统入口解析策略事前闭合，不得由 post-load inventory 追认 |
| `W3.CUSTOM.ACTUAL_MODULE_REPROOF` | proof后到load间swap与loaded-module path/FileId复核 | 实际module逐项等于pre-load retained identity；不一致失败 |
| `W3.CUSTOM.HANDOFF_ONE_SHOT` | 缺失、重复take、错误线程/解释器、伪造对象、跨进程/序列化重放、close后复用 | 只有同进程compiled-in producer的单次opaque authority可用，状态严格`CREATED→TAKEN→CLOSED` |
| `W3.CUSTOM.MANIFEST_EXACT` | manifest digest、offset/count/overlap/overflow、非法UTF-8/路径、duplicate entry/basename、dependency cycle、unknown flag、extra/trailing bytes tamper | parser、embedded pre-link root绑定及post-build release binding全部fail closed |
| `W3.CUSTOM.SOURCE_EXACT_BYTES` | critical source proof后swap/tamper | executed digest等于retained-handle digest和manifest；swap不能改变bytes |
| `W3.CUSTOM.SOURCE_ONLY` | 同名PYZ/`.pyc`/`__pycache__` duplicate | build与runtime均拒绝；执行loader仅为`TrustedSourceLoader` |
| `W3.CUSTOM.FIXTURE_EXACT_BYTES` | fixture missing/tamper/swap | 只从retained handle读取，digest/identity错误失败 |
| `W3.CUSTOM.NONREPO_CWD` | 无checkout、净化`PATH/PYTHONPATH/Qt`、非仓库CWD | spike正常PASS且无checkout/CWD read |
| `W3.CUSTOM.FAILURE_DIAGNOSTIC` | windowed pre-Python、bootstrap、loader各阶段失败 | 产生稳定无正文诊断marker，进程非零退出，不静默成功 |
| `W3.CUSTOM.REPRODUCIBLE_INPUTS` | 两个clean build重放toolchain lock | input/runtime/release manifest与patch一致；PE无Security Directory，Debug Directory只有单一`IMAGE_DEBUG_TYPE_REPRO`且无CodeView/PDB/其他entry；两个PE/dist bytes完全一致，否则失败 |

1.6只有上述全部mandatory断言在同一custom entry build上PASS才完成。普通PyInstaller build成功、Qt窗口出现、source lane通过或单一positive smoke均不能替代此矩阵。

## 7. 维护与升级边界

- PyInstaller、CPython minor/patch ABI、compiler/SDK、bootloader patch、应用 PE imports、Python C API 表、manifest schema、应用系统调用 allowlist 或 pre-authority module/dynamic roots 变化，都重建应用 TCB 并至少重跑 1.6 矩阵。
- 只变Windows PowerShell 5.1 Build/Revision不改变W3 TCB；它是evidence orchestrator事实，由`CLEAN_WINDOWS_V2`记录而不阻断。PowerShell edition/major/minor/architecture、system-host选择或行为profile变化仍阻断证据lane。
- Task 1.6只裁决最小机制可行性；完整Qt/PySide6/SQLite/native closure、owner roots、resources和全业务E2E仍归Task 7～10，不得由最小spike冒充。
- upstream若接受等价实现，仍须按新source/digest重跑；不得仅因“已upstream”降低release-owned review和attack matrix。
