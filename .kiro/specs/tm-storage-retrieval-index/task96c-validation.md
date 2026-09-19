# Task 9.6c 受信输入与发布验收

结论：已完成 WA-06 R4 的构建前 Core 消费实现。以下保留原验收事实；提交整合没有重新签发 runtime 或 frozen 资格。

## 已验证的能力

- Core 内部建立 source 输入会话，Gate A/C、Matcher 与 Gate D 消费同一 owner 的输入和证明窗口；正式 Matcher factory 支持私有 session，source 公开入口继续兼容。
- 结果发布与取消共用短内存提交边界；可失败的输入复证、窗口退出及 native 收尾在引用安装前完成。取消先发生则零安装，提交先完成则后续撤销不倒判已完成结果。
- 下游通过 Core 登记的普通对象引用槽成套提交，拒绝只读 native 字段及任意 setter；原观察锁覆盖安装和恢复。接口见[发布合同](trusted-input-publication.md)。

## 原验收范围

最终 publication 修复经独立审查接受。父代理 Core/source/worker/Gate D 影响面共 245 项，9 项既有跳过；Host 兼容回归 96 项、零跳过，两组均 exit 0。另有 6 项真实路径反例及独立审查的 13 项反例通过。静态增量 37→37，无新增诊断；底层工具 exit 1，不是全仓零错误。

9 项跳过为 retained source mutation 1 项、POSIX publisher 故障注入 6 项、Windows retained run-root handle 2 项；均未计为通过。测试中的合成测量只证明控制流，不证明性能。

对应回归入口：`tests.test_tm_gate_inputs`、`tests.test_tm_gate_input_composition`、`tests.test_tm_gate_contract_inputs`、`tests.test_tm_gate_publication`。原始命令、输入快照、失败与复验记录保存在本地过程归档。

真实 native producer、Feature5/Qt 消费和同候选 100k 资格由各 owning task 验收；本报告不构成完整 frozen PASS。
