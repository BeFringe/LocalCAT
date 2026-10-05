# Brief: cross-device-sync-plugin

## Problem

个人译者希望在自己的设备间继续项目，并复用翻译记忆库和术语表。直接同步工作目录会传播半写入文件、设备状态和凭据，并可能覆盖并发编辑。

## Current State

LocalCAT 已有独立的 ProjectPackage 与 JSONL/CSV ResourcePackage 导出、验证及导入事务，但没有远程 provider、同步计划或冲突协议。资源包已有受限传输句柄，项目包需要提供等价的整包出口。远端同步应消费这些本地合同。

## Desired Outcome

用户配置自己的远端，手动预览并执行上传、下载和删除。冲突保留双方，失败可恢复，本地项目不依赖网络可用性。先完成 R2，再验收 InfiniCLOUD WebDAV；不引入官方云账号。

## Approach

提供可启停的同步模块、独立 provider adapter 和基于共同基线的计划。provider 只处理 bytes/metadata，项目和资源继续由各自 owner 验证和应用。参考 Remotely Save 的自有存储配置和计划交互，独立实现 LocalCAT 的包传输协议；凭据参考 Kiss Translator 的默认遮蔽、点击显示交互。

## Scope

- **In**：R2、InfiniCLOUD WebDAV、用户远程前缀、手动 push/pull/two-way、只读计划、冲突副本、删除保护、逐项恢复、连接设置、凭据遮蔽与显式显示。
- **Out**：内容端到端加密、定时/后台同步、实时协作、云端 TM、语义自动合并、动态安装第三方代码、chunk 权限同步。

## Boundary Candidates

- 本地 artifact adapter 对接各 owner 的完成出口和导入事务；
- provider 只实现列举、读取、条件写入/删除和安全元数据；
- planner 拥有 base/local/remote 关系及用户决策；
- 凭据保存在设备秘密后端，连接配置与项目内容分离；
- Qt 只呈现 Controller 的计划、冲突、进度和结果。

## Out of Boundary

不解析 package manifest 或 codec 私有成员，不搬运 live SQLite、journal、stage、设备 key 或 Fuzzy 资格，不将远端变为项目权威，不按 mtime 静默覆盖。不提供加密选项；星号遮蔽仅控制界面显示，传输仍须 HTTPS。

## Upstream / Downstream

- **Upstream**：`rpy-project-codec` 的手工项目闭环、`multi-document-project-workspace`、`language-resource-portability`、平台持久化合同。
- **Downstream**：个人跨设备继续工作；未来 provider 扩展不改变项目与资源语义。

## Existing Spec Touchpoints

- **Extends**：Project 整包传输出口；Application 的可禁用同步配置与生命周期；平台秘密存储及受限网络依赖的组合。
- **Adjacent**：ResourcePackage import/apply、Qt 手动入口、已有协作分工。它们不由同步插件重新定义。

## Constraints

先验证手工包，再接网络。凭据不进入项目、日志或 Git；实际 endpoint、bucket、用户名和秘密均由用户本机配置。条件更新能力不足时明确禁用危险操作，不冒称兼容。删除和覆盖必须经计划确认，远端超时不等于操作未发生。

## Promotion Clusters

1. 本地 artifact 接缝、设备配置/秘密存储与禁用生命周期。
2. R2 provider、远端版本协议与计划/冲突。
3. owner apply、失败恢复与 Qt 手动工作流。
4. InfiniCLOUD WebDAV 能力验证与两设备闭环。
