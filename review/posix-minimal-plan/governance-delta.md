# 最小治理差异与 owning 关系

本候选在 ADR-033 采纳后由治理线承载；它不把功能实现、验收结果或本轮审批过程写进 steering。

| 文件 | 最小差异 |
| --- | --- |
| `.kiro/steering/adr/adr-033.md` | 采用本包 `adr-033.draft.md` 的决策正文；状态由实际 ADR 审批决定。 |
| `.kiro/steering/adr/README.md` | 新增 ADR-033 条目；在 ADR-007、008、020 索引状态标明对应限定，保留其他已采纳决策。 |
| `.kiro/steering/adr/adr-007.md`、`adr-008.md`、`adr-020.md` | 仅追加／更新「取代关系」指向 ADR-033 的精确条款；不重写历史决策正文或旧日期。 |
| `.kiro/steering/delivery-boundaries.md` | 加入下列支持环境与检查边界。 |
| `.kiro/steering/tech.md` | 在数据与安全边界加入下列 SQLite 与应用命名操作区别；不预写新模块、驱动或已实现事实。 |
| `.kiro/steering/spec-ownership.md` | 对本次四 owner 的窄 amendment 登记当前开发线血缘，按 owner 独立审阅和语义提交；不是转移 TM/平台/资源/UI authority。 |

`delivery-boundaries.md` 候选正文：

> TM 在受支持的稳定本机目录中保证正常使用、LocalCAT 自己协调的并发／资源替换／崩溃恢复、资源包导出导入及释放全部使用者后的正常迁移。运行中外部移动或替换活动 TM 目录、替换活动数据库或直接干预内部数据库／事务恢复文件不受支持，不承诺任意同用户竞争下全时序零副作用；已可观察的绑定失效仍须拒绝，检测后停止受影响资源的新操作、保留恢复事实，不接管同路径副本。配置 JSONL／manifest 的外部内容变化仍按既有 SOURCE_DIVERGED 合同处理，canonical 与其位置仍可证明时保留原 Lookup/Update。该限定不削弱恶意包、路径穿越、链接、摘要篡改、正常输入校验及应用自身 rooted 发布。
>
> 位置复证是资源操作内部检查，不是用户资格、长基准或新验证入口。Windows 保持普通 frozen，Mac/Linux 保持 source；检索兼容性按实际执行输入变化决定复验，纯文档和 Git 元数据变化不触发产品全量重验。

`tech.md` 候选正文：

> TM 的通用 rooted/lock/publish backend 与 Windows 专用恢复能力分别组合。应用自行执行的资源命名和清理消费原 retained root 与同资源家族上下文；标准 SQLite 在经过操作边界复证的稳定命名空间中工作，前后复证不构成其全部内部 I/O 的目录句柄绑定。保留既有驱动、POSIX v2／Windows v3 格式及 coordinator；不通过清空 backend 绕过授权，也不将 opaque 文件身份解码为持久权威。

`spec-ownership.md` 的当前开发线窄补充：

> `maintenance/project-codec-adjacency` 可承载 ADR-033 对应的 Core、共享平台、ResourcePortability 与 Feature 5 UI amendment；各 owner 继续独占自己的 R/D/T、运行时合同和验证结论，按 owner 分别形成语义提交。范围限于 POSIX TM 分派、同资源家族生命周期、配置位置复证、rooted 应用发布、资源级失效投影及其实际交付回归。同步只消费原包事务入口；不因此开放 Core 检索、持久格式、全局平台或其他 UI 重构。

不改 `repository-safety.md`：当前规则已要求治理先行、原链远端保全、精确区间重放和 lease。也不顺带刷新 `product.md`／`roadmap.md` 的旧发行状态；本轮最终能力确有新增时再按原 RPY6.3 收尾核对实际事实。
