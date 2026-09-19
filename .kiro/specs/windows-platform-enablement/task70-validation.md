# Task 7.0 构建前消费集成验收

结论：已集成 Core 9.6c/9.6d 与 Feature5 3.6a 消费合同，并建立 `packaging/windows/frozen_roots.json` 的 owner roots、Gate 输入、动态 import、worker 和资源声明。

原独立审查接受；父代理 Host 受信输入 9 项、Qt 生命周期 19 项均零跳过、exit 0，声明、Gate 摘要与 contract 检查通过。独立 source 121 项、Qt 48 项零跳过，Core 121 项有 1 项 retained-handle 注入跳过；均 exit 0。四个任务核验脚本的静态检查有 2 项类型诊断、实际 exit 1，原审查判定非阻断；不得记为零诊断。

owner 集成定位见 [ledger](cross-spec-amendments.md)。原始命令、输出和审查记录保存在本地过程归档。提交整合不重签原运行事实。

本任务未验收 native producer、完整候选、100k 性能或 Windows Feature GO；普通发行按 ADR-028 重新设计。
