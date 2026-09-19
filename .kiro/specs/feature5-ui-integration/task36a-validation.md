# Task 3.6a 后台消费与 Qt 生命周期验收

结论：已完成 WA-07 3.6a 的构建前消费实现，消费 Core 9.6c/9.6d 合同并保持 source 行为。以下保留原验收事实，不授予完整 frozen 资格。

Host handoff、操作状态、通知 generation 与 Core snapshot 经同一引用计划提交；close 先撤销实际 Core owner，再在短锁外唤醒等待者。Qt 只在 owner 线程处理 native 生命周期，排队 terminal、关闭与后台等待不形成互相等待。source publication 的失败仍完整恢复 prior。

独立审查接受后，父代理 source 121 项、Qt 48 项均零跳过；Core 输入/worker/publication 121 项有 1 项既有跳过；另有 5 项真实接口反例通过，均 exit 0。静态增量 81→81，无新增诊断，底层工具 exit 1。

跳过项是 retained handle 阻止的 source mutation 注入，不计为通过。反例中的合成 measurement 只验证提交与撤销，不证明 100k 性能。

对应回归入口：`tests.test_capability_host_frozen_inputs`、`tests.test_capability_publication_retry`、`tests.test_qt_owner_dispatch`、`tests.test_qt_frozen_composition`、`tests.test_qt_publication_retry`。原始输入、命令、输出和审查记录保存在本地过程归档。

平台 7.2 producer、同候选 Qt/业务/100k 及最终发行仍未验收；普通 frozen 的替代接线按 ADR-028 设计。
