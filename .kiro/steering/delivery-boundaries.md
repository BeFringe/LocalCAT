# 交付与验证边界

更新：2026-09-20。本清单用于 source 与 frozen 的设计、实施和评审；只记录长期边界，实验过程和本机工作目录另行保存。

## 交付顺序

- Windows source 已完成用户自管 CPython 3.14 x64、专用 venv、源码与轻量 launcher 的产品闭环。W3 不再充当 Windows 平台迁移或 source 交付的前置门。
- Windows 首次 frozen 是 0.5.2 自包含发行目标，最终用户无需另装 Python、venv 或 Qt。普通打包、Qt/资源/SQLite 烟测和同一候选上的真实 Project/TM/TMX/FTS5 用户旅程、必要数据保护、Core Gate C/D 验证组成一个交付闭环；实施可以分步，产品验收不分拆，尚未验收的包不能称为已交付。
- source 的通过不证明 frozen 通过；frozen 的未完成也不撤销 source 成果。各自按交付范围记录状态。

## 保留与重估清单

| 类别 | 保留的结果与边界 | 收窄或重估的证明 |
| --- | --- | --- |
| 保护用户数据 | rooted 访问、拒绝重定向、活句柄身份、跨进程锁、候选写入与发布、读回和崩溃恢复；私有密钥与资格记录的用户权限检查 | Windows 按 Win32/SID/ACL/MIC 实现，不模拟 POSIX mode、inode 或目录 fsync；私有存储的严格 profile 不自动扩展到普通程序资源 |
| 证明检索语义与性能 | Core 拥有 Gate C 语义、Gate D 性能、确定性排序和能力发布；保留现有门限、资格失配后的诚实状态、取消与正常关闭 | 分开记录检索耗时、迁移耗时和启动验证成本；先查重复扫描与验证生命周期，不把证明开销误诊为 scorer 性能 |
| 证明程序来源与启动基础 | 可信发行输入、固定依赖与构建清单、执行产物与 owner evidence 的绑定、必要的入口与加载路径检查、真实包内业务验收 | 普通 frozen 不强制定制 native entry、retained-source-only loader、完整 Boot TCB 逐事件证明或同用户任意替换竞态防御；具体取代范围见 ADR-028 |

## 不随发行方式改变的业务边界

- Project/ProjectPackage、ResourcePackage、TM 与 Parser 各自保留业务合同、事务和恢复职责；平台只提供文件、锁、发布与私有存储端口。
- TMX 直接导入继续使用 Parser 的 sealed source；TMX 导出支持资源、项目与选定分工范围；TMX ResourcePackage 仍为 export-only，不因此开放 package import/apply。
- JSONL/ResourcePackage 搬运数据，不搬运 Fuzzy capability。设备本地资格仍按当地运行时和实现重新证明或恢复。
- source/frozen 均不能凭 `sys.frozen`、任意 PASS 文件、未执行的旁置源码或 mock capability 开放能力。

## 增加检查之前

每项新增阻塞应能说明保护的用户结果、适用威胁和成本；优先在构建或发布阶段验证稳定事实，仅对实际可变状态做运行时复证。不可解释的“更严格”不自动成为交付要求。

已批准的边界可以因产品目标重新裁决。保留决策历史与明确取代关系即可，不为普通局部选择增立 ADR，也不把旧实现的成本当作继续扩张证明的理由。数据损坏、结果错误和已声明能力的回归仍须修复；已撤回的更强安全声明不继续驱动普通产品验收。
