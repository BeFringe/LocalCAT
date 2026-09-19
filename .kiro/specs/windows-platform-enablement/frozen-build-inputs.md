# W3 构建输入与生成器接口

本文件记录 Task 7.1 已接受的强证明路线接口；绑定及作用域补充见[绑定规则](frozen-build-bindings.md)和[作用域规则](frozen-build-scopes.md)。普通 frozen 按 ADR-028 重新设计，不自动继承下方 source-only 与完整 native 证明要求。

## 当前交付

`tools/generate_windows_frozen_manifest.py` 只读取文件和 AST，不导入待收录业务模块。开发模式从当前 Git worktree 的原 owner 声明、Gate A/C path 投影、Core benchmark tuple 和 worker mapping 得到真实闭包；保留 Core 原输入字节，不重算或重签 Core aggregate digest、fingerprint、Gate 或 worker codec。生产模式额外要求严格生产输入合同，默认即为生产模式。

输出目录必须尚不存在，生成器写出 `frozen-source-manifest.json` 和 `hooks/hook-capability_host.py`。该 hook 只含本次算出的 `module_collection_mode = {module: 'py'}`，由 7.3 的产品 spec 消费。输出是构建目录内的派生产物，不是手写或提交后维护的另一份真相。Task 7.1 evidence 中的副本仅是可复核的历史生成结果。

```powershell
& '<固定Python>/python.exe' -B tools/generate_windows_frozen_manifest.py `
  --repository '<活动worktree>' --output '<新的build目录>' --mode development

& '<固定Python>/python.exe' -B tools/generate_windows_frozen_manifest.py `
  --repository '<干净worktree>' --output '<新的build目录>' `
  --input-root 'wheels=<隔离wheel目录>' `
  --input-root 'python=<隔离Python目录>' `
  --input-root 'native=<隔离native目录>' `
  --input-root 'boot=<隔离bootloader目录>' `
  --input-root 'toolchain=<隔离toolchain目录>'
```

任一缺失、漂移、歧义或非法输入返回 exit 2，不先写不完整产物。开发输出明确标记 `DEVELOPMENT_CLOSURE_NOT_RELEASE`；它允许 tracked source 工作字节与 HEAD 不同，但列出 dirty path，仍拒绝 untracked 实际项目输入。正式生产要求 clean tracked tree，并把每个实际源码输入与 `HEAD:path` 原始字节逐一比较，因此 `assume-unchanged` 不得隐藏实际输入漂移。无关的既有未跟踪持久 lock 不进入闭包，不会被删除。

## 生产输入合同

7.3 在 `packaging/windows/build_inputs.json` 提供版本控制的 JSON，顶层精确字段为：

| 字段 | 合同 |
| --- | --- |
| `schema_version` | `localcat-frozen-build-inputs-v1` |
| `build_sources` | `repository-relative-path -> {bytes, sha256}`，包括实际 spec、bootstrap、ico、version、build requirements lock、此生成器及所有构建 helper。生成器递归解析 Python imports，要求真实 transitive helper 被登记。整个 `packaging/windows/` 定义目录必须入清单，排除 plan 自身、owner roots（另已绑定）与版本化 release scenario 数据。 |
| `input_trees` | `id -> {kind, files: {relative-path: {bytes, sha256}}}`。每个真实目录必须与声明闭集完全相同；拒绝多项、缺项、reparse、重叠目录、bytecode路径及摘要不符。kind 必须覆盖 `locked-wheels`、`python-runtime`、`native-runtime`、`bootloader`、`build-toolchain`。 |
| `runtime_imports` | `owner-declared-module -> tree-id/relative-path`。集合须覆盖真实 owner roots 和构建源第三方 imports；模块路径必须对应实际 wheel member 的 `.py`、`__init__.py` 或 `.pyd`。 |
| `wheel_members` | `tree-id/relative-path -> {wheel: tree-id/archive.whl, member: archive-relative-path}`。对真实 wheel ZIP 逐项验证 RECORD、成员闭集与 SHA，再比较展开后的 runtime 字节，不能只自报 hash。 |
| `native` | 下述独立输入图。 |
| `resulting_pe` | 未来最终 EXE 的相对路径，仅作为排除项，不把它或它的摘要放进自己的 pre-link 输入。 |

生成器自动绑定 plan 自身、owner roots、实际执行的 generator 字节与派生 hook 字节。hook 不反向包含 manifest digest，所以没有循环摘要。外部 input tree 的绝对位置只由 CLI locator 提供，不成为 manifest identity；它们是脱离 ambient venv 的隔离 staging 副本，不能指向 checkout 内未跟踪目录或直接扩大到共享环境。持久原始下载材料仍留在活动 worktree；必要 staging 副本不能替代原始材料。

固定必需源路径是 `packaging/windows/LocalCAT.spec`、`frozen_source_bootstrap.py`、`packaging/windows/LocalCAT.ico`、`packaging/windows/version_info.txt`、`packaging/windows/requirements-build.lock` 与本生成器。实际生产文件由 7.2/7.3 提供；本任务没有创建假 spec、假 bootstrap 或产品正例。

## 原生图和源码图分离

`native` 的精确字段：`entries`、`dynamic_roots`、`python_dll`、`bootloader`。`entries` 是 `tree-id/path -> {bundle_path, dependencies: [tree-id/path...]}`；成员须与真实 isolated trees 中全部 `.dll`、`.pyd` 及 bootloader `.exe` 一致。每项均由实际 bytes 派生 SHA；校验路径重复、缺失引用、基本 MZ 输入类型和独立 DAG，动态根必须引用已登记成员，Qt plugin roots 必须实际存在。Python 导入图允许 SCC，不送入此 DAG。

这里的 `graph_kind` 固定为 `native-input-dag-not-runtime-attestation`。**本校验不是 PE 静态/delay/API-set/forwarder 闭包证明，MZ 检查也不是有效 PE 判定。** 7.2 必须从真实 PE 与已锁定来源形成这些输入关系，完成 producer/native容量/Boot TCB/retained handle/执行证明与完整 1.6 mandatory 重验后，才能用它们铸造 runtime authority。不得把隔离 Git fixture 的合成输入合同测试当作任何 W3 PASS。

## 实际收集结果验证

`validate_collection(generated, actual_members, pyz_modules=actual_toc)` 对真实收集结果验证必需 `.py`/fixture/data存在且摘要一致，拒绝额外关键源副本、`.pyc`、`__pycache__` 和关键 PYZ duplicate。后续 packager 必须从真实 archive 取得 TOC，不能把手写空列表当证明。输入 Python ZIP/wheel 也扫描关键源码/bytecode副本。post-build 的完整 dist/PE binding 和 `TrustedSourceLoader` executed-byte proof 仍由原有 release owner 与 7.2/7.4 完成。

## 验证范围

真实源图保留 Windows 当前 runtime 不加载的 POSIX fingerprint 源文件，其 stdlib 名称记录为 `stdlib-source-reference`；不能据此声称 Windows 已加载 `fcntl` 或 `resource`。未知项目/第三方依赖、未声明或无界 dynamic call、反射/alias import 均拒绝，Core Gate D 的当前动态调用只在 AST 中存在原有顺序集合校验且校验紧邻调用时接受。

Task 7.1 不改变 owner 源、阈值、Qt composition、Core codec、旧 spike codec、系统环境或用户 WIP；没有运行完整 W3、实际 packaged candidate 或最终 Windows GO。
