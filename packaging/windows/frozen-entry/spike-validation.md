# W3 最小 frozen spike 验收记录

状态：独立组合审查已批准，下表全部 14 项 mandatory 断言为 PASS；Task 1F／1.6 完成。

本记录是实施后的证据索引，不修改 W3 设计合同。范围仅为已批准的同进程 custom `runw`、保留初始化源码、一个 critical module 和一个 fixture；不是 Qt/PySide6/SQLite 全应用 frozen 发行，也不关闭 Task 7～10。

## 同一候选的外部锚

| 对象 | 值 |
|---|---|
| clean 构建提交 | `75390ade487f15c8a2aef243e81af7f2650485c7` |
| candidate input digest | `22df2bb748f1cadac1daf4410c06be5be18bf3e1b2395259e15e9d97160a1e84` |
| candidate lock SHA-256 | `596b2406a4cc863b717960f441809b328c7b834b79b768710735dbef267ff172` |
| 两次独立构建的 release SHA-256 | `6b95a2736a21d42e76a7a0c60b4e4f4c4721f78cf733341720d896e4f2523a4e` |
| 两次最终 EXE SHA-256 | `2b6dba85a9369a3cf842d3bc66ecbe1432c7b3ee221661af172e56665d7b76ea` |
| realized-build lock SHA-256 | `36086d480ad537debf51cd6afef03c58da8cec0317fa75ff5bdc3b6be76efe8d` |

构建 `f` 与 `g` 的完整 dist 和构建准备文件逐字节一致，且不共享成员文件身份。后续观察工具提交不改变该候选：每次实际运行仍使用以上 release、commit、candidate 三个调用方外锚，并在运行前后复核完整发行物。

`native-component-10bf40cb4def` 保留锁定 MSVC/SDK 下原 native 测试的输入快照、实际编译／执行命令、原始输出和产物。其 `result.json` SHA-256 为 `f73375fe1f2974fb749c96c30f986fd7d1522834e49baa2415c75d2bffb568ac`。它还核对历史 `d` 调用点审查与本次 `f`：native 模板及相应实际应用源码一致，最终 `.text` 与 `.pdata` 的 RVA、virtual size 和完整字节一致；Python DLL／VCRuntime 仍使用各自完整摘要。该桥接允许复用原调用点分析，不把旧 candidate 的清单遗漏当作已通过。

## 证据层次

- 构建与代码审查证明应用输入、调用点、递归依赖及路径切断条件。运行时保留句柄证明在加载／读取之前建立，不从调试器事后清单授予 authority。
- 最终 PE 的实际轨迹检验该实现的正常与具体失败支线；未命中不表示不可达。受控返回值／状态故障与真实线程、真实文件操作分别标记。
- KnownDLL 注册表是已观察的系统 seed；child API-set 数据与返回模块路径是系统解析事实。捕获发生在静态映射之后，不称为 pre-load enforcement。这些观察值沿用既定 OS 信任边界，不转为随包输入或用户启动条件。
- API-set 的实际名称与捕获 schema 有版本差异时，只记录唯一 hash-prefix 相关及真实调用的返回 host，不实现或假定通用 Windows resolver。

## Mandatory 断言与组合证据

下列 ID 均省略公共前缀 `W3.CUSTOM.`。原始本机路径、日志及可重放脚本留在 `artifacts/windows/`；这些不是产品依赖。

字节索引为 `artifacts/windows/final-index-draft/index.json`，当前 SHA-256：`0456540000a94a81edf9f21404286feb7eee5f755714d108a92700e2883c76a5`。同目录 `collect_index.py` 重读已选报告及其原始输出、调试脚本、编译缓冲区和构建输入引用，再核对 `f`／`g` 完整发行物。索引的 `SOURCE/` 表示证据工作树，`BUILD/` 表示保存 clean build 的工作树；Git 中不保存本机绝对路径。该索引只检验字节引用链，不重新执行构建、不自行给断言投 PASS，也不代替独立的轨迹语义审查。

| 断言 | 实现与证据组合 | 必须保留的限定 |
|---|---|---|
| `PE_SYSTEM_ONLY` | realized-build 的 imports／manifest；[应用调用闭包](application-loader-closure.md)；`system-resolution-mtfuf4ny` 与实际 loader 配对轨迹 | 系统观察不替代应用调用点事前审查 |
| `DLL_POLICY_FIRST` | `loader-canary-vhxmvl4i` 的有效 DLL 及真实重定向正控；原生 E1→dispatcher 时序；CRT 兼容回退的独立污染组合 | policy 之前的 CRT 系统调用不能借 E1 放行 |
| `BUNDLE_ROOTED` | `retained-window-3ofjsk_z` 的 root／ancestor／internal junction、hardlink 及两个保留窗口；rooted native 反例 | 有限标签与布局，不声称穷尽所有 NTFS reparse tag |
| `NATIVE_CLOSURE_PRELOAD` | `payload-verification-jh18p6vv`；保留窗口；manifest 与 `localcat_native_closure.c` 的逐成员 prove-before-load | 当前最小闭包不包含完整 Qt native 树 |
| `DYNAMIC_ROOTS_DECLARED` | 应用 loader 全引用枚举、固定 roots 与切断条件；系统入口事实；正常／失败／统计／CRT fallback 轨迹 | 不把 OS 内部服务路径增列为应用输入 |
| `ACTUAL_MODULE_REPROOF` | 最终 PE 的实际 dispatcher 与保留窗口；rooted native 的错误 actual-module 路径反例 | 组件故障与最终成品操作分层记录，不声称所有 swap 均实际成功发生 |
| `HANDOFF_ONE_SHOT` | 同 PE 的 bootstrap 真对象／异线程自检；identity 五项；availability 三项 | identity／premint 部分为单进程受控故障；跨进程不可传递由不可构造、无反序列化入口和真对象协议拒绝共同证明 |
| `MANIFEST_EXACT` | native parser hostile-fixture 矩阵；最终 PE 的 embedded digest tamper；release 外锚反例 | 组件 parser 矩阵不是对最终 PE 逐项重签攻击 |
| `SOURCE_EXACT_BYTES` | `source-execution-a4zzeiux` 的完整编译字节与代码对象→eval；payload tamper；保留窗口 | 不把仅摘要相同替代实际 compile 输入关联 |
| `SOURCE_ONLY` | 实际 retained source loader；payload 的 PYZ／pyc／pycache 等额外输入拒绝 | 未使用 stock bytecode 路径追认源码 |
| `FIXTURE_EXACT_BYTES` | fixture missing／tamper；保留窗口；critical module 的实际 retained read | 只覆盖 manifest 声明的最小 fixture |
| `NONREPO_CWD` | System32 CWD 正控；E9 布局和文件观察；初始化源码的路径切断 | 文件观察是交叉核验，不独自证明所有 I/O 不可达 |
| `FAILURE_DIAGNOSTIC` | E8 NULL、E9 分配／retained 失败、availability 拒绝、E9 统计退出和 Win32 标准句柄状态故障 | NULL 状态不等同于所有 CRT 描述符为空；观察到 UI fallback 调用，但为避免弹窗受控返回，不冒称真实显示 |
| `REPRODUCIBLE_INPUTS` | `f`／`g` 的 clean 构建与 `realized-build-10fqucnz` | 字节重现不是其余加载／执行断言的替代品 |

额外 file-symlink 变体在本机因缺少 `SeCreateSymbolicLinkPrivilege` 记录为 `BLOCKED_NOT_RUN`，没有实测通过。已有最终成品的 junction 布局覆盖与一律拒绝 `FILE_ATTRIBUTE_REPARSE_POINT` 的同一 native 检查共同支持 rooted 边界；不要求用户为本 spike 改 Windows 权限或逐个枚举所有 reparse tag。

## 组合裁决与后续边界

独立 reviewer `audit_crt_failure_exit` 已复核上述冻结索引及原始证据，并对 `custom-entry-matrix.json` 中全部 14 个有序 ID 分别裁决 PASS。E9 九个最终路径场景绑定已审提交 `5bd9ba7`，CRT flags=0 污染组合绑定 `bfb5ab9`；native 组件及历史调用审查通过输入／机器码桥接关联同一最终 PE。索引仍保持 `NOT_ADJUDICATED_BY_INDEX`：字节核验与独立语义裁决是两件事。

Task 7～10 继续负责完整 Qt／PySide6／SQLite 依赖、业务资源闭包和发行环境 E2E。应用输入、调用清单、运行时或工具链变化时，依已批准计划重新构建并验收；本记录不是后续产物的通用放行凭据。

## 历史重组后的构建与字节桥接

上述历史提交、release 和原始索引均保留原锚。历史重组没有重新执行或改签全部 W3 反例；新构建与原验收候选的关联由独立复核的字节桥接建立，不通过批量替换旧日志中的提交摘要建立。

| 新构建对象 | 外部锚 |
|---|---|
| clean 实现提交 | `a616c8dcd9c992028ca39153b257df4aa68640bc` |
| 两次独立 clean build 的 release SHA-256 | `3708f7e35a38928894b79e4b38a6239cd34cabf5218e75868c0dd753560387e1` |
| 新 realized-build lock SHA-256 | `252facf7b60ef154cb26745f1adb2229ac4f23522b04fcfd7d464b2f8bd59239` |
| history-bridge.json SHA-256 | `42d6d60e3df1b86c490f3fdfe5df848ea42b456665996994fa0392d0876d79d3` |

新记录位于重组构建工作树的 `artifacts/windows/rewrite-validation/history-bridge.json`；两次构建为同树 `artifacts/windows/r13a/custom-runw-g44ncelp` 和 `artifacts/windows/r13b/custom-runw-r0d0wq7d`，各自父目录保留 release 与 receipt。新 realized lock 位于 `artifacts/windows/realized-build-pj4zan4t/`。桥接中的 `SOURCE`、`BUILD` 仍指原证据树与原 clean 构建树，`REWRITE` 指新构建树；不将相同相对路径误当作同一份证据。

两次新构建实际执行 Waf、封装和两种标准句柄条件下的 GUI 子进程；新旧完整发行文件与构建准备文件逐字节相同，新两棵树不共享成员文件身份。candidate、补丁、native/source/fixture、工具和测试的非文档 Git blob/mode 与原终态完全相同；实际 PE/API/native 事实重新计算后与原 realized lock 一致。旧索引和全部引用字节也已重新核对，原 file-symlink 的权限限制仍保留。

release 摘要变化来自新的提交和来源库存绑定，不代表运行产物变化。上表锚定的是完成实现的 clean 提交，后续验收文档提交不追称为同一次构建的源提交。本桥接只承接同一最小候选的既有验收，不关闭 Task 7～10，也不扩大产品或系统信任边界。
