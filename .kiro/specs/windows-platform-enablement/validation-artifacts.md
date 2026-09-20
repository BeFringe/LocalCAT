# W3 构建前能力的验收依据

下表记录强证明路线已完成的构建前能力。报告保留原验收范围及跳过限制，提交整理不重新签发 runtime、性能或 frozen 资格。

| 范围 | 历史验收事实 | 接口或回归入口 |
| --- | --- | --- |
| Core 9.6c | [受信输入与发布](../tm-storage-retrieval-index/task96c-validation.md) | [发布合同](../tm-storage-retrieval-index/trusted-input-publication.md)；`tests.test_tm_gate_inputs`、`tests.test_tm_gate_publication` |
| Core 9.6d | [独立 worker](../tm-storage-retrieval-index/task96d-validation.md) | `tests.test_tm_benchmark_worker`、`tests.test_tm_benchmark_process`、`tests.test_tm_benchmark_query_process` |
| WA-07 3.6a | [后台消费与 Qt 生命周期](../feature5-ui-integration/task36a-validation.md) | `tests.test_capability_host_frozen_inputs`、`tests.test_qt_owner_dispatch`、`tests.test_qt_frozen_composition` |
| 平台 7.0 | [消费集成](task70-validation.md) | `packaging/windows/frozen_roots.json` |
| 平台 7.1 | [清单生成](task71-validation.md) | [构建接口](frozen-build-inputs.md)；`tests.test_windows_frozen_source_generator` |
| 平台 7.2 | 未完成，仅有 producer 构建接口检查点 | `tests.test_windows_frozen_producer`、`tests.test_windows_frozen_producer_inputs` |

用项目 CPython 3.14 环境从仓库根执行对应 unittest 模块可重新验证相关实现，例如 `python -B -m unittest tests.test_tm_gate_inputs tests.test_tm_benchmark_worker`。目标候选仍须实际构建并运行 owning Gate 和用户旅程。

原始日志、输入快照、失败诊断和审查过程保存在被忽略的本地 `artifacts/windows/`；普通 clone 不含这些归档。此索引不承担本机归档路径和 ZIP 摘要管理，也不把历史报告当作普通 PyInstaller 路线的验收结果。
