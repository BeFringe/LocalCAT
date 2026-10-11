# 被拒绝候选的复核输入

这些文件只用于解释已撤回候选，不能应用到活动开发树或用户数据。当前生产基线仍安全拒绝该导入。原观察运行时 HEAD 为 `07a12b7ed551fb495259dd9f6feec6414c467a6c`，叠加当时未提交候选；当前 `2e1cbce` 只作为后续复核基线，不能改签原运行。两个提交的 `tm_migration.py` 与既有锁断言测试基线字节相同。

- `unsafe-candidate.patch` 是原候选的完整补丁；未因分享重写。
- `test_tm_resource_snapshot_posix.py` 是合成 owner 测试辅助文件，包含后来增加的真实 builder 目录替换负向断言。
- `parent_probe.py` 来自原独立评审，仅把仓库定位改为第一个命令行参数；分享版未重新签发原产品结论。
- 上一级 `rejected-candidate-observations.json` 提取原日志的完整 JSON 观察和终端失败断言，省去本机 traceback 路径；不是当前基线运行结果。

需要复核时，仅在基线 `2e1cbce96a27b13b42f89f73c0eabe47dbd3cdc5` 的可丢弃独立 checkout 中应用补丁，将此测试文件放入其 `tests/`，再执行 `python parent_probe.py <该独立checkout>`。它会新建临时合成资源并清理，不使用用户资源。运行时和平台必须另行记录；其他环境不能预先承诺得到同一结果。本交接没有应用该补丁或重复运行完整业务测试。
