# Task 9.6d 独立 worker 验收

结论：已完成 WA-06 R4 的构建前 worker 消费实现。以下保留原验收事实；提交整合不产生新的 frozen 或性能资格。

source 使用独立 migration/query 模块子进程；每个 child 自建输入窗口，保留严格 request/result codec、独立 PID、timeout 和失败语义。frozen 消费端选择同候选 EXE 的固定模式及定向二进制管道；实际 native producer 的接入另行验收，不回落 venv 或进程内执行。

RSS 在结果准备、完整预编码及最终输入复证后采集 OS 自启动峰值，并同步结果和摘要。原验收覆盖四条真实消费路径以及 `512 MiB + 1 byte` 拒绝；migration elapsed、PID、门限和 schema 未改变。

首次 worker 验收的独立完整组 144 项、Gate/codec 33 项均零跳过、exit 0；父代理相关组 106 项、零跳过、exit 0。Core publication 最终修复后，Core/source/worker/Gate D 影响面 245 项及 Host 96 项再次通过；前者 9 项既有跳过及其范围见[9.6c 验收](task96c-validation.md)，不计为通过。首次静态增量 49→49，无新增；底层工具 exit 1，不是全仓零错误。

对应回归入口：`tests.test_tm_benchmark_worker`、`tests.test_tm_benchmark_process`、`tests.test_tm_benchmark_query_process`。原始输入、命令、输出和审查记录保存在本地过程归档。

本报告只验收 pre-build 消费合同；实际 E10 进程、同候选 100k 双路径和完整 frozen 产品仍未通过。
