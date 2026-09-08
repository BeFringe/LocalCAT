# Windows 启动优化复测对照

本文是 Task 16 的调查索引，不是 Requirements/Design 的补充授权或完成报告。保留独立机制的提交，回归后以同口径测量判断下一步，不依赖对话记忆，也不按提交标题推定性能收益。

## 测量边界

- 分别记录真实入口触发、SOURCE bootstrap、首个可交互窗口、资源就绪、后台能力验证完成；首屏与资源就绪均不能代替完整后台验证。
- Start Menu 点击到就绪包含 launcher 和 Python 进程启动；`bootstrap_import → resources_ready` 不包含此前时间。`child_exit` 是进程存活时间，不是启动时间。
- SOURCE 与 frozen 分列；Mac/Linux/Windows 固定同一代码提交、同一数据库内容与资源标志。记录实际注册表的资源数量、类型、canonical/legacy 状态、条目数和大小；非活动资源也可能被检查。不得只按启用 TM 的条目数解释差距。
- 区分新进程启动与文件系统冷缓存；同口径取多次样本并保留原始值、版本、入口和配置摘要。内部 profiling 用于归因，最终比较使用不带逐调用探针的真实入口。
- inclusive span 不相加；启动时间与后台工作量分开报告。偶发 `CHILD_FAILED` 另记结果，不能静默删除失败样本。

## 机制与复测入口

提交号仅为整理后历史的检索锚点，不是运行时 authority。每行的收益判断有自己的证据边界，不能相加得到总收益。

| 机制与提交 | 实现定位 | 已知信息／证据限制 | 回归后优先验证 |
|---|---|---|---|
| TM 装配去重：`787c638` | `editor_controller.py` 的 `_build_tm_engine_set_for_runtime_snapshot`；`tm_engine.py` 的 `from_open_canonical_store` | 去掉兼容装配的第二次完整开库；没有独立同口径的单提交时延 A/B | 每个 canonical/legacy 资源开库次数；查询、确认写回仍使用同一有效 owner |
| 索引复用边界：`15070ee`、`f84ec5f` | `tm_sqlite_store.py`；`tm_snapshot_artifacts.py` | 历史探针出现单库三次全验，后续变为一次；不同配置/探针的耗时不能直接相减 | hydration/health 的全验次数；本进程当前 generation 复用；内容、身份、权限变化和新 coordinator 的重新验证 |
| 最终路径探测：`3bf1233` | `platform_fs_windows.py` 的 `_final_path` | 普通路径从先查长度再读取改为一次读取；无独立整机时延对照 | 原生调用次数、长路径扩容和失败语义；不能用路径缓存替代活身份 |
| 内容捕获与 W1 锁：`816a2a2`、`3d58f48` | `BoundRegularFile.capture_content`；`_WindowsBoundRegularFile`；`_read_lock_payload` | 减少重复读取、目录遍历以及无写入取得中的 flush；历史组合探针不能拆为两个提交的准确收益 | read/hash/目录复证/flush 次数；已有锁与首次初始化、strict-prefix 恢复分开测量 |
| 同步 SOURCE 证明：`b88699c` | `platform_source_authority.py`；`capability_host.py` | 历史装配探针相邻阶段中位数约 2.42 → 2.36 秒；只是几十毫秒级趋势，不是提交级 A/B | 同一同步窗口的根/文件证明次数；窗口外、文件漂移和关闭后不得复用 |
| 同次冷开内容事实：`ed98648` | `tm_activation_recovery.py`；`tm_migration.py`；`tm_sqlite_store.py`；`tm_engine.py` | 相应实现期间装配探针约 2.36 → 1.92 秒；是组合调查区间，不是 Start Menu 总时延或严格单提交 A/B | recovery/current DB/terminal owner 各阶段；完整 SHA-256、活身份、锁、私有安全与失效拒绝仍成立 |
| 源码锚点与编译：`1f710c3`、`27240ac` | `capability_host.py` 的 Gate C/D 源码证明与后台资格流程 | 历史记录的 capability 阶段 627 → 119 ms、bootstrap 到资源就绪 3.269 → 2.743 秒；累计包含延后编译，不能拆成两个提交的收益，也不表示后台全部完成 | AST/compile 次数与所属线程、资源就绪和 Gate C/D 完成各自时间；共享锚点复用与后台 fresh proof 不混淆 |
| 首页与首帧：`af82509`、`fecd47a` | `qt_editor.py`；`qt_startup_window.py` | 首屏响应与异步交接改进，不等于首次开库或完整启动已达标 | 真实首帧、可交互状态、资源交接、失败/关闭；Windows 空入口之外的既有顺序不回退 |

输出锁清理、ResourcePackage/TMX 导出、安装路径拒绝和文档提交不纳入启动性能排名；它们按各自功能和安全边界回归。

## 历史证据与易误读处

- capability 数字来自旧备份历史 `44c4b3d` 对 Qt `tasks.md` 的修改。其测量说明可检索，但原来据此勾选 Task 16 的结论不成立；当前 Task 16 仍未完成。该历史记录不是本次重测或跨平台基准。
- 本地历史探针位于 `artifacts/windows/source-startup-profile/`，属忽略的调查产物，不保证出现在新 checkout；本表保留其有用结论，缺少原始产物时必须重新测量，不据摘要补造 evidence。
- `isolated-current-run.py` 的 `editor_composition` 包含内部 TM 工作；其 `measure()` 对同名 `tm_open`/`tm_status_probe` 字段覆盖而非累加，多 TM 场景只留下最后一次调用，不能把该字段称为全部 TM 总耗时。复测若沿用探针，应记录每资源 span 或明确累计字段。
- `isolated-current-actual-phased`、`after-*`、`source-window-*`、`tm-fastpath-*`、`terminal-owner-*` 是开发期间的阶段样本，不带完整的单提交 A/B 绑定。相邻样本、原生调用计数和 CPU 时间可支持定位，不能直接作为发布性能承诺。

## 回归后的执行顺序

1. 固定通过回归的提交和资源配置，分别取得 SOURCE 的入口到首屏、资源就绪、后台能力完成基线；如已有 frozen candidate，另建同口径一列，不与 SOURCE 混测。
2. 先比较总时延，再按 launcher/Python import、Repository authority/lock、TM runtime、capability、Qt 首帧归因。模型负责沿实现与测试定位，计时和调用计数负责证实。
3. 只对仍占显著时间或具有明确重复工作的机制做候选优化；以同资源、同入口、同采样方式做前后对照，并检查所属 Spec 的拒绝/恢复语义。
4. 每个独立且完整的优化形成一个提交；小收益若引入过高维护成本，评估是否保留实现，不通过合并历史隐藏成本。未完成真实启动验收前不勾选 Task 16。
