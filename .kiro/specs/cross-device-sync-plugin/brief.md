# Brief: cross-device-sync-plugin

## Problem

LocalCAT 的项目、翻译记忆库、术语表和工作区状态目前只保存在一台设备。个人译者希望在桌面与其他设备之间继续工作，但内置云账号会破坏本地优先定位，直接同步工作目录又可能泄露凭据、覆盖并发修改或传播半写入文件。

## Current State

LocalCAT 没有插件宿主、远程 provider、同步日志或跨设备冲突协议。Remotely Save 展示了可参考的本地优先模式：远程服务只充当 broker，支持 S3/WebDAV 等 provider、手动/定时同步、可选端到端加密、增量 push/pull 和基础冲突处理；它同时强调备份、敏感配置保护和大文件限制。

参考资料：

- <https://github.com/remotely-save/remotely-save/blob/master/README.md>
- <https://github.com/remotely-save/remotely-save/blob/master/docs/sync_algorithm/v3/intro.md>
- <https://github.com/remotely-save/remotely-save/blob/master/docs/minimal_intrusive_design.md>
- <https://github.com/remotely-save/remotely-save/blob/master/LICENSE>

## Desired Outcome

LocalCAT 可安装一个独立、可禁用的跨端同步插件。用户选择自己的远程 provider，预览待上传、下载、删除和冲突，再显式同步经批准的项目包与资源。同步失败保留本地可用状态，凭据不进入项目、日志或版本控制；没有插件时 LocalCAT 保持完整的纯本地工作流。

## Approach

定义稳定的同步插件边界、provider 能力与 opaque transport snapshot/index，由插件协调本地已批准 package bytes、远程 listing、差异计划、冲突决策和提交。首批优先 S3-compatible 与 WebDAV，其他 provider 后续独立扩展。借鉴 Remotely Save 的 provider、增量同步、保护阈值和可选加密思路，但不复制其 Obsidian 状态模型，也不依赖其 PRO 代码；插件不得另造 ProjectPackage/ResourcePackage manifest authority。

## Scope

- **In**: 可安装/禁用插件；S3-compatible/WebDAV provider；远程前缀；手动同步；push-only/pull-only/two-way；同步计划预览；新增/修改/删除；冲突保留双方；保护阈值；失败恢复；可选加密；敏感凭据保护；大文件/路径过滤；本地同步日志。
- **Out**: LocalCAT 官方云账号；实时多人共同编辑；chunk 分配与权限；云端 TM 服务；自动解决翻译语义冲突；复制 Remotely Save PRO 能力或源代码。

## Boundary Candidates

- Core 只发布可同步 snapshot/package 与原子导入导出，不感知具体远程 SDK；
- plugin host 管理安装、权限、版本和 provider capability；
- provider 只负责远程列举、读写、删除和 metadata；
- sync planner 产生只读计划，用户批准后才执行；
- conflict record 保留 local/remote/base 信息，不以 modified time 直接静默覆盖；
- chunk metadata 只有在协作规格以及新的 ProjectPackage schema/version 或明确 extension 共同批准后才可作为可运输成员；strict v1 不预埋该字段，chunk 语义始终归协作规格。

## Out of Boundary

- 不把同步成功等同于实时协作或分布式锁；
- 不把远程副本设为项目唯一权威；
- 不同步明文 access key、secret、token 或加密密码；
- 不在当前 Qt JSON、Feature 5 或 Parser 实施波次中加入远程依赖；
- 不承诺后台进程在应用关闭时继续同步。
- 不同步或导入 ADR-013 的 `gate-d-qualification`、`device.key`、`qualification.json`；Fuzzy qualification 是设备本地运行资格，另一设备必须独立恢复兼容的本地 attestation 或重新验证。
- 不直接复制 live canonical SQLite sidecar、journal、stage residue 来铸造资源 authority；同步只消费 Core 批准的资源 export/import package 与 receipt。

## Upstream / Downstream

- **Upstream**: `multi-document-project-workspace` 的稳定 ProjectPackage、原子保存和 source reconciliation；`language-resource-portability` 批准的 ResourcePackage 与资源 import/apply transaction。
- **Downstream**: 多设备个人工作流；未来可选择同步 `collaborative-job-chunks` metadata，但不提供协作权限。

## Existing Spec Touchpoints

- **Extends**: 本地优先产品边界与未来插件宿主。
- **Adjacent**: `collaborative-job-chunks` 定义协作范围；本规格仅同步文件和 metadata。

## Constraints

同步前必须建议或验证可恢复备份；任何批量删除或覆盖都要经过保护阈值和显式确认；凭据应进入操作系统安全存储或等价秘密后端；插件停用或卸载后本地项目仍可正常打开。

Remotely Save 的 `src/tests/docs/assets` 采用 Apache-2.0，而 `pro` 目录采用 PolyForm Strict；LocalCAT 只参考公开行为与文档，不复制 PRO 实现。

## Promotion Clusters

1. plugin host、opaque transport snapshot/index 与本地 package import boundary；
2. provider capability、planner、凭据与加密；
3. apply/recovery、冲突保护和操作日志；
4. Qt 预览/确认与真实 provider acceptance。

项目包必须先由 `multi-document-project-workspace` 冻结；可选 chunk metadata 还必须等待 `collaborative-job-chunks` 的 schema/permission clusters。同步实现不得为了抢跑后续规格而在当前单 JSON 项目或 workspace 中预埋第二套 authority。
