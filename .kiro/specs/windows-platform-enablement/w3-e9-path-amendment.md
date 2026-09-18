# W3 E9 路径初始化窄范围修订

**状态：已获人工批准，待实现验收。** 本文补充 `w3-custom-entry-plan.md` 的 E9 合同，授权下述唯一 C API、启动源码与时序增量及其派生输入更新；不代表正式 runtime 或完整 frozen spike 已通过验收。

## 目标与边界

本修订归 `windows-platform-enablement` 的 W3 entry 合同，覆盖 10.1、10.3、10.5、10.6 的初始化路径隔离，以及 12.4 的可重放证据。目标是在正常和失败路径上，都不让解释器按未授权目录搜索或执行启动代码。

保持已锁定 CPython、同进程 custom runw、E4/E5 retained proof、真实随机哈希种子和 E10 one-shot handoff。不得改成父 wrapper、修改 Python DLL、关闭 importlib、固定随机种子，或以初始化后的检查追认先前访问。本修订不代答应用 native 闭包、真实 runw 构建及完整 mandatory matrix。

## 具体增量

| 边界 | 已批准合同 |
|---|---|
| E8 C API | 仅新增锁定 DLL 的兼容 ABI 导出 `void (*Py_SetPath)(const wchar_t *)`。该函数已从现代公开 C API 移除，不能写成新推荐 API；清空和检查真实 `sys.path` 使用既有通用对象 API，不新增 getter、PyList API 或私有分阶段初始化 API。 |
| E9 原生配置输入 | DLL policy 仍是第一段 LocalCAT entry 动作。在 Python DLL 加载前拒绝存在的 `__PYVENV_LAUNCHER__`；不修改用户或系统环境。program_name/executable 来自已绑定的真实 EXE，不接受 argv、CWD 或环境替代值。 |
| getpath 自动探测 | 非空 `Py_SetPath` 输入只允许一个已绑定 bundle 根绝对路径，拒绝分隔符造成多个搜索项的布局；不得使用空串、空路径项或伪造 executable。此值只为关闭自动探测，不能成为 source authority。 |
| 首次路径清空 | 用 `PyInitConfig_AddModule` 注册 compiled-in `_frozen_importlib_external` 适配器，在任何该模块源码执行、PathFinder/zip hook 安装前检查并清空真实 `sys.path`。未命中、重复、错误线程/状态或清空失败均终止；不得等待 encodings 或初始化返回。 |
| E5 启动源码集合 | 新增锁定 stdlib 的 `Lib/importlib/_bootstrap_external.py`，作为 exact-byte interpreter entry；其原生适配器从保留句柄复验、读取和直接编译，与现有 encodings 支持同域。不得改成磁盘 SourceFileLoader 或采用未绑定的冻结代码替身。 |
| 安装职责 | 适配器仅返回同名模块；仍由原有 frozen importlib core 注入 `_bootstrap` 并调用其 `_install`。保持单次安装、模块对象同一性及原有依赖，不引入第二套导入器安装流程。 |
| 失败诊断 metadata | E9 retained interpreter 源码使用带尖括号的、由 manifest entry id 派生的逻辑编译名，避免 CPython 异常打印器按 co_filename 重开文件。实际源路径、digest 和 live identity 仍由 manifest/retained proof 绑定；不改变 E11 bootstrap/critical source 的来源合同。 |
| 初始化后不变量 | 搜索路径继续为空；isolated/no environment/no site/no user site/safe path/no bytecode/不解析 argv 保持启用。显式关闭可选 faulthandler、tracemalloc、profiling，并保持 xoptions/warnoptions 为空。Py_SetPath 对 prefix 的结果不作 bundle authority，也不得转为 CWD 查找。 |

选择兼容导出的代价必须单独接受：[CPython 已在 3.13 移除其公开 API](https://docs.python.org/3.14/whatsnew/3.13.html)，[锁定版本的实现](https://raw.githubusercontent.com/python/cpython/v3.14.7/Python/pathconfig.c)仍为 stable ABI 保留它。该接口返回 void，内部路径分配失败会直接触发 fatal error，并非可检查返回值的初始化失败。现有 PEP 741 显式搜索路径不能等价阻断 getpath 的额外探测；本候选不因此降级 Python 或扩展到其他旧配置接口。任何 runtime 变更都须重新核验该导出、签名与实际顺序，不能把 ABI 存在当作行为不变保证。

## 失败边界

- 路径设置调用前先记录稳定原生阶段状态，再尝试可选输出；stderr 或调试器不可用不阻止初始化。`Py_SetPath` 内部 fatal/OOM 以该阶段和非零进程终止诊断，由验收观察者取证，不承诺返回调用方后再输出失败标记或无人观察时持久化。不得吞掉 fatal、继续初始化或恢复成普通路径查找；此分支仍须故障验证。观察通道与最小 E8 表按 `w3-custom-entry-plan.md` §5.3。
- 能返回调用方的清空前失败：磁盘查找器尚未安装，拒绝进入 retained 源码执行或 handoff，产生稳定原生失败标记并终止。CPython 自有的早期错误打印仍须验证，不能假定它不存在。
- 清空后失败：不得恢复临时路径、改用 frozen/disk fallback 或继续 handoff。保留源码的语法、执行、安装与重证失败均撤销 authority；CPython 自身的异常输出也纳入路径访问验证。
- 初始化成功：再次检查隔离结果只是确认不变量，不补授先前访问的 authority。E10 仍须独立核验所有 E4/E5/E7 事实。

## 文件责任与重新验证

- `native/localcat_frozen_bootstrap.c/.h`：原生配置、早期适配器、保留源码执行与失败撤销；目录相对 `packaging/windows/frozen-entry/`。
- `packaging/windows/frozen-entry/candidate-contract.json`、输入生成器及派生 lock：加入唯一新增 API 和 interpreter source，并重建内容绑定；不手改摘要冒充已实现输入。
- native 初始化测试：保留现有 launcher/父级配置失败回归，加入新落点、故障注入与源码/metadata 反例；诊断程序不能替代最终 runw。
- W3 计划及 Task 1.6 证据：同步 exact delta，最终仍要求同一真实 custom entry 的全部 mandatory 断言。

必须重新验证：空 `Py_SetPath` 负控及其 fatal/OOM 阶段诊断；父级 pyvenv 与 DLL/EXE 邻接 `._pth`；环境污染；bundle 路径分隔符；首次适配器身份和仅安装一次；清空前后失败；真实 `_install` 失败；启动源码 tamper/duplicate/执行错误；逻辑编译名不触发磁盘重开；最终候选的文件访问及 native load 时序。

## 当前证据与尚未覆盖的范围

独立诊断已区分两个落点：encodings 入口清空的显式异常打印回放能执行外部 traceback 正文；early-external 在相同回放及 retained `_install` 故障 fixture 中保持外部正文标记未出现。另验证了父级配置、`._pth`、环境污染、单次安装和清空失败。故障 fixture 是受保留句柄约束的测试输入，不是正式 stdlib 内容。

这些证据支持继续审查该候选，不等于正式 E9 或 Task 1.6 已完成。尚需将获批合同接入真实 retained runtime 装配、覆盖全部源码失败与布局反例、绑定 exact build，并完成 E0～E11。调试器轨迹只是观测；软件断点影响时序，单次没有观测到调用不能证明不可达。

## Governance Impact

- 遵循 ADR-022/023 的唯一 authority、完整 Boot TCB 与 pre-authority 时序；不提出取代 ADR。
- W3 exact API、interpreter source 集合和编译 metadata 仅按本文增量更新；既有诊断不能替代获批实现的重新验证。
- 不改变 Core、TM、Qt 或 POSIX 语义，不要求 Steering 改写。重新生成 W3 candidate 输入，重验 Task 1.6；Task 7 的完整 runtime 闭包不得沿用旧 E9 证据。
