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

定义稳定的同步插件边界、provider 能力与项目包清单，由插件协调本地 snapshot、远程 listing、差异计划、冲突决策和提交。首批优先 S3-compatible 与 WebDAV，其他 provider 后续独立扩展。借鉴 Remotely Save 的 provider、增量同步、保护阈值和可选加密思路，但不复制其 Obsidian 状态模型，也不依赖其 PRO 代码。

## Scope

- **In**: 可安装/禁用插件；S3-compatible/WebDAV provider；远程前缀；手动同步；push-only/pull-only/two-way；同步计划预览；新增/修改/删除；冲突保留双方；保护阈值；失败恢复；可选加密；敏感凭据保护；大文件/路径过滤；本地同步日志。
- **Out**: LocalCAT 官方云账号；实时多人共同编辑；chunk 分配与权限；云端 TM 服务；自动解决翻译语义冲突；复制 Remotely Save PRO 能力或源代码。

## Boundary Candidates

- Core 只发布可同步 snapshot/package 与原子导入导出，不感知具体远程 SDK；
- plugin host 管理安装、权限、版本和 provider capability；
- provider 只负责远程列举、读写、删除和 metadata；
- sync planner 产生只读计划，用户批准后才执行；
- conflict record 保留 local/remote/base 信息，不以 modified time 直接静默覆盖；
- chunk metadata 可作为项目包成员，但 chunk 语义仍归协作规格。

## Out of Boundary

- 不把同步成功等同于实时协作或分布式锁；
- 不把远程副本设为项目唯一权威；
- 不同步明文 access key、secret、token 或加密密码；
- 不在当前 Qt JSON、Feature 5 或 Parser 实施波次中加入远程依赖；
- 不承诺后台进程在应用关闭时继续同步。

## Upstream / Downstream

- **Upstream**: `multi-document-project-workspace` 的稳定项目包、原子保存和 source reconciliation；资源仓储的可验证导入导出。
- **Downstream**: 多设备个人工作流；未来可选择同步 `collaborative-job-chunks` metadata，但不提供协作权限。

## Existing Spec Touchpoints

- **Extends**: 本地优先产品边界与未来插件宿主。
- **Adjacent**: `collaborative-job-chunks` 定义协作范围；本规格仅同步文件和 metadata。

## Constraints

同步前必须建议或验证可恢复备份；任何批量删除或覆盖都要经过保护阈值和显式确认；凭据应进入操作系统安全存储或等价秘密后端；插件停用或卸载后本地项目仍可正常打开。

Remotely Save 的 `src/tests/docs/assets` 采用 Apache-2.0，而 `pro` 目录采用 PolyForm Strict；LocalCAT 只参考公开行为与文档，不复制 PRO 实现。
